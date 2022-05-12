from functools import reduce
from operator import mul

import numpy as np
import torch
import torch.nn as nn
from timm.models.layers import to_3tuple

HU_INTENSITY_INTERVALS_LC = np.array([
                            -1000, # Air
                            -650,  # Lung
                            -75,   # Fat
                            0,     # Water/Fluids
                            15,    # Cerebrospinal Fluid
                            30,    # Muscle/Kidney
                            50,    # Liver / Blood
                            80,    # Acute (Clotted) Blood
                            450,   # Trabecular Bone
                            1000   # Cortical Bone
                            ])

HU_INTENSITY_INTERVALS = np.array([
                            -1000,
                            # Air
                            -900,
                            # Lung
                            -400,
                            # ?
                            -100,
                            # Fat
                            -50,
                            # ?
                            -10,
                            # Water/Fluids
                            20,
                            # Muscle/Kidney
                            40,
                            # Liver/Blood
                            60,
                            # Acute (Clotted) Blood
                            100,
                            # Trabecular Bone
                            800,
                            # Cortical Bone
                            1000
                            ])



class LearnedClassVectors(nn.Module):
    def __init__(self, patch_size, out_dim, vector_dim, 
                 intensity_transform=None, 
                 sincos_emb=False,
                 final_layer=False, 
                 concat_vector=False,
                 linear_comb=False):

        super().__init__()

        self.patch_size = to_3tuple(patch_size)
        self.final_layer = final_layer
        self.vector_dim = vector_dim
        self.out_dim = out_dim
        self.voxels_per_patch = reduce(mul, self.patch_size)
        self.sincos_emb = sincos_emb
        self.concat_vector = concat_vector
        self.linear_comb=linear_comb

        if self.linear_comb:
            self.org_intervals = HU_INTENSITY_INTERVALS_LC
        else:
            self.org_intervals = HU_INTENSITY_INTERVALS

        if not intensity_transform is None:
            intensity_intervals = intensity_transform(self.org_intervals)
            self.intensity_intervals = np.unique(intensity_intervals)
        else:
            self.intensity_intervals = self.org_intervals

        if self.linear_comb:
            self.n_vectors = len(self.intensity_intervals) - 1
        else:
            self.n_vectors = len(self.intensity_intervals) + 1

        if self.final_layer and self.concat_vector:
            assert self.vector_dim == self.n_vectors
            self.fc = nn.Linear(self.vector_dim, self.out_dim)
        elif self.final_layer:
            self.fc = nn.Linear(self.voxels_per_patch * self.vector_dim, self.out_dim)
        else:
            assert self.voxels_per_patch*self.vector_dim == self.out_dim

        self.vectors = nn.ParameterList()
        self.vectors_cls = nn.ParameterList()
        self.cls_val = -10000

        if not self.sincos_emb:
            for i in range(self.n_vectors):
                if self.concat_vector:
                    interval_param = nn.Parameter(torch.zeros(self.vector_dim), requires_grad=False)
                    interval_param[i] = 1.0
                    self.vectors.append(interval_param)
                else:
                    self.vectors.append(nn.Parameter(torch.randn(self.vector_dim)))
                self.vectors_cls.append(nn.Parameter((torch.ones(1)*(i+1)*self.cls_val).repeat(self.vector_dim), requires_grad=False))

        if self.linear_comb:
            self.vectorized_v_to_w = np.vectorize(self.voxel_to_weight)


    def forward(self, x):
        B, C, D, H, W = x.size()
        Pd, Ph, Pw = self.patch_size

        if self.linear_comb:
            voxel_vectors = self.create_voxel_vectors_linear_comb(x)  # n_patches * (Pd * Ph * Pw), vector_dim
        elif self.sincos_emb:
            voxel_vectors = self.create_voxel_vectors_sincos(x) # n_patches * (Pd * Ph * Pw), vector_dim
        else:
            voxel_vectors = self.create_voxel_vectors(x) # n_patches * (Pd * Ph * Pw), vector_dim

        voxel_vectors = voxel_vectors.view(B, C, D, H, W, self.vector_dim).squeeze(1) # B, D, H, W, vector_dim assuming C=1
        voxel_vectors = voxel_vectors.permute(0, 4, 1, 2, 3).contiguous() # B, vector_dim, D, H, W
        patches = voxel_vectors.unfold(2, Pd, Pd).unfold(3, Ph, Ph).unfold(4, Pw, Pw) # B, vector_dim, D/Pd, H/Ph, W/Pw, Pd, Ph, Pw
        if self.concat_vector:
            patch_vectors = patches.sum(-1).sum(-1).sum(-1) # B, vector_dim, D/Pd, H/Ph, W/Pw
            patch_vectors = patch_vectors.permute(0, 2, 3, 4, 1).contiguous() # B, D/Pd, H/Ph, W/Pw, vector_dim
        else:
            patches = patches.permute(0, 2, 3, 4, 5, 6, 7, 1).contiguous() # B, D/Pd, H/Ph, W/Pw, Pd, Ph, Pw, vector_dim
            patch_vectors = patches.flatten(4) # B, D/Pd, H/Ph, W/Pw, (Pd * Ph * Pw * vector_dim)

        if self.final_layer:
            patch_vectors = self.fc(patch_vectors)

        patch_vectors = patch_vectors.permute(0, 4, 1, 2, 3).contiguous()

        return patch_vectors


    def create_voxel_vectors(self, x):
        x = x.flatten(0)
        x = x.view(-1,1)

        x = torch.where(x < self.intensity_intervals[0], self.vectors_cls[0], x)
        for i in range(0, (self.n_vectors - 2)):
            x = torch.where((x >= self.intensity_intervals[i]) & (x < self.intensity_intervals[i+1]), self.vectors_cls[i+1], x)
        x = torch.where(x >= self.intensity_intervals[-1], self.vectors_cls[-1], x)

        for i in range(self.n_vectors):
            x = torch.where(x == (i+1)*-10000, self.vectors[i], x)

        return x

    def create_voxel_vectors_sincos(self, x):
        x = torch.clamp(x, min=self.intensity_intervals[0], max=self.intensity_intervals[-1])
        x = x.flatten(0)
        x = x.view(-1,1)

        x = self.get_hu_sincos_embed(x)

        return x


    def create_voxel_vectors_linear_comb(self, x):
        x = torch.clamp(x, min=self.intensity_intervals[0], max=self.intensity_intervals[-1])
        x = x.flatten(0)
        x = x.view(-1,1)

        x_lb = x
        x_ub = x

        # Create lower bound vectors
        for i in range(0, (self.n_vectors - 1)):
            x_lb = torch.where((x_lb >= self.intensity_intervals[i]) & (x_lb < self.intensity_intervals[i+1]),
                               self.vectors_cls[i], x_lb)
        x_lb = torch.where(x_lb == self.intensity_intervals[-1], self.vectors_cls[-2], x_lb)

        for i in range(self.n_vectors):
            x_lb = torch.where(x_lb == (i + 1) * self.cls_val, self.vectors[i], x_lb)

        # Create upper bound vectors
        for i in range(0, (self.n_vectors - 1)):
            x_ub = torch.where((x_ub > self.intensity_intervals[i]) & (x_ub <= self.intensity_intervals[i + 1]),
                               self.vectors_cls[i + 1], x_ub)
        x_ub = torch.where(x_ub == self.intensity_intervals[0], self.vectors_cls[1], x_ub)

        for i in range(self.n_vectors):
            x_ub = torch.where(x_ub == (i + 1) * self.cls_val, self.vectors[i], x_ub)

        # Create weights
        x_w = self.vectorized_v_to_w(x.cpu().numpy())
        x_w = torch.from_numpy(x_w).cuda()

        # Resulting vector for each voxel is a weighted linear combination of the upper and lower bound vectors
        x = x_w*x_ub + (1-x_w)*x_lb

        return x

    def voxel_to_weight(self, voxel):
        indx = np.searchsorted(self.intensity_intervals, voxel)
        if not indx:
            indx = indx + 1
        weight = (voxel - self.intensity_intervals[indx - 1]) / (self.intensity_intervals[indx] - self.intensity_intervals[indx - 1])
        return weight

    def get_hu_sincos_embed(self, voxel_values):
        """
        voxel_values: Voxel values to be transformed into vectors
        out: (vector_dim, 1)
        """
        assert self.vector_dim % 2 == 0
        voxel_values = (voxel_values*2)-1

        omega = 2 ** torch.arange(self.vector_dim // 2).float().cuda()

        omega *= torch.pi
        res = omega * voxel_values

        emb_sin = torch.sin(res)  # (M, D/2)
        emb_cos = torch.cos(res)  # (M, D/2)

        emb = torch.concat([emb_sin, emb_cos], axis=0)  # (M, D)
        return emb