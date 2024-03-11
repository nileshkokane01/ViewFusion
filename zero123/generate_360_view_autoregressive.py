import os
import diffusers  # 0.12.1
import math
import lovely_numpy
import lovely_tensors
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import rich
import sys
import time
import torch
from contextlib import nullcontext
from diffusers.pipelines.stable_diffusion import StableDiffusionSafetyChecker
from einops import rearrange
from functools import partial
from ldm.models.diffusion.my_ddim import DDIMSampler
from ldm.util import create_carvekit_interface, load_and_preprocess, instantiate_from_config
from lovely_numpy import lo
from omegaconf import OmegaConf
from PIL import Image
from rich import print
from transformers import AutoFeatureExtractor #, CLIPImageProcessor
from torch import autocast
from torchvision import transforms
from torch.utils.data import Dataset, DataLoader
import json
import pandas as pd
import random
import matplotlib.pyplot as plt
import torch.multiprocessing as mp

_GPU_INDEX = 0

def load_model_from_config(config, ckpt, device, verbose=False):
    print(f'Loading model from {ckpt}')
    pl_sd = torch.load(ckpt, map_location='cpu')
    if 'global_step' in pl_sd:
        print(f'Global Step: {pl_sd["global_step"]}')
    sd = pl_sd['state_dict']
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print('missing keys:')
        print(m)
    if len(u) > 0 and verbose:
        print('unexpected keys:')
        print(u)

    model.to(device)
    model.eval()
    return model


@torch.no_grad()
def sample_model(input_im, model, sampler, precision, ddim_steps, scale, ddim_eta, T, interpolation_weight=None):
    precision_scope = autocast if precision == 'autocast' else nullcontext
    with precision_scope('cuda'):
        with model.ema_scope():
            batch_size, _, h, w = input_im.shape
            c = model.get_learned_conditioning(input_im)
            T = T.unsqueeze(1)
            c = torch.cat([c, T], dim=-1)
            c = model.cc_projection(c)
            cond = {}
            cond['c_crossattn'] = [c]
            cond['c_concat'] = [model.encode_first_stage((input_im.to(c.device))).mode().detach()]
            if scale != 1.0:
                uc = {}
                uc['c_concat'] = [torch.zeros(batch_size, 4, h // 8, w // 8).to(c.device)]
                uc['c_crossattn'] = [torch.zeros_like(c).to(c.device)]
            else:
                uc = None

            shape = [4, h // 8, w // 8]
            samples_ddim, _ = sampler.sample(S=ddim_steps,
                                             conditioning=cond,
                                             batch_size=batch_size,
                                             shape=shape,
                                             verbose=False,
                                             unconditional_guidance_scale=scale,
                                             unconditional_conditioning=uc,
                                             eta=ddim_eta,
                                             x_T=None,
                                             interpolation_weight=interpolation_weight)
            print(samples_ddim.shape)
            samples_ddim = samples_ddim.mean(dim=0, keepdim=True)
            x_samples_ddim = model.decode_first_stage(samples_ddim)
            return torch.clamp(x_samples_ddim, min=-1., max=1.0)


class RenderingDataset(Dataset):
    def __init__(self):
        self.meta = []

        with open('/home/ubuntu/data/GSO/rendering_test_list_video_condition_id.json', "r") as f:
            model_dict = json.load(f)
        for model_name in model_dict:
            inference_id = model_dict[model_name]
            meta_dict = {
                "model_name": model_name,
                "inference_index": int(inference_id),
                "inference_image_path": f"/home/ubuntu/data/GSO/gso_renderings_elevation60/{model_name}/{int(inference_id):03d}.png",
            }
            self.meta.append(meta_dict)
        self.transform = transforms.Compose([
            transforms.Resize((256, 256)),
            transforms.ToTensor(),
        ])
    def __len__(self):
        return len(self.meta)

    def __getitem__(self, idx):
        meta_info = self.meta[idx]
        inference_image_path = meta_info['inference_image_path']
        model_name = meta_info['model_name']
        inference_index = meta_info['inference_index']

        inference = self.load_filtered_img(inference_image_path)
        inference = inference * 2 - 1

        return inference, model_name, inference_index
    
    def load_filtered_img(self, image_path, color=[1., 1., 1., 1.]):
        img = plt.imread(image_path)
        img[img[:, :, -1] == 0.] = color
        img = Image.fromarray(np.uint8(img[:, :, :3] * 255.))
        img = img.convert("RGB")
        img = self.transform(img)
        return img

class RealDataset(Dataset):
    def __init__(self, data_dir='/home/ubuntu/workspace/zero123/3drec/data/real_images/'):
        self.meta = []
        for model_name in os.listdir(data_dir):
            meta_dict = {
                "model_name": model_name.split('.')[0],
                "inference_index": 0,
                "inference_image_path": f"{data_dir}/{model_name}",
            }
            self.meta.append(meta_dict)
            
        self.transform = transforms.Compose([
            transforms.Resize((256, 256)),
            transforms.ToTensor(),
        ])
    def __len__(self):
        return len(self.meta)

    def __getitem__(self, idx):
        meta_info = self.meta[idx]
        inference_image_path = meta_info['inference_image_path']
        inference_mask_path = inference_image_path
        model_name = meta_info['model_name']

        inference = self.load_filtered_img(inference_image_path, inference_mask_path)
        inference = inference * 2 - 1

        return inference, model_name
    
    def load_filtered_img(self, image_path, mask_path):
        image = Image.open(image_path).convert("RGB")
        # mask = Image.open(mask_path).convert("1")
        mask = Image.open(mask_path).split()[-1]

        # Apply background filtering
        image_filtered = Image.new("RGB", image.size, color=(255,255,255))
        image_filtered.paste(image, mask=mask)
        image_filtered = self.transform(image_filtered)
        return image_filtered
    
def main_run(models, device, dataset, inference_temp, auto_temp, output_dir, scale=3.0, ddim_steps=75, ddim_eta=1.0, precision='fp32', h=256, w=256):
    '''
    :param raw_im (PIL Image).
    '''
    
    dataloader = DataLoader(dataset, batch_size=1, shuffle=False)
    sampler = DDIMSampler(models['turncam'])
    
    for data in dataloader:
        # inference, model_name, env_index = data
        inference, model_name, env_index = data
        if os.path.exists(f"{output_dir}/{model_name[0]}/{env_index[0]}/"):
            print("Exist", model_name[0])
            continue
        # batch_size = inference.shape[0]

        # 360 degree
        degree_steps = 36
        autoregressive_list = [(inference, 0)]
        for view_id in range(degree_steps-1):
            rotation_cond_tmp = torch.zeros(len(autoregressive_list), 4)
            
            if view_id&1:
                view_angle = -2*np.pi/degree_steps*(view_id//2+1) # view_id 1, 3, 5 ,7, 9, ..., 33
            else:
                view_angle = 2*np.pi/degree_steps*(view_id//2+1) # view_id 0, 2, 4 ,6, 8, ..., 34
            

            anchor_view_angle_list = np.array([auto_reg[1] for auto_reg in autoregressive_list]).astype(np.float32)
            rotation_cond_angle = view_angle - anchor_view_angle_list
            print(view_id, view_angle/np.pi*180, rotation_cond_angle/np.pi*180, anchor_view_angle_list)
            
            interpolation_weight = np.where(np.abs(rotation_cond_angle)<np.pi, np.abs(rotation_cond_angle), 2*np.pi-np.abs(rotation_cond_angle))
            print("angle_offset:", interpolation_weight/np.pi*180)
            valid_index = interpolation_weight<125/180*np.pi
            valid_index[0] = 1
            print("valid_index:", valid_index)

            interpolation_weight = torch.from_numpy(interpolation_weight)[valid_index]
            if view_id>=1:
                inference_weight = np.exp(-(view_id+1)/degree_steps/inference_temp)
                interpolation_weight[1:] = torch.nn.functional.softmax(-interpolation_weight[1:]/interpolation_weight[1:].max()/auto_temp)*(1-inference_weight)
                interpolation_weight[0] = inference_weight
            else:
                interpolation_weight[0] = 1
            print("interpolation_weight:", interpolation_weight)
            interpolation_weight = interpolation_weight.reshape(sum(valid_index), 1, 1, 1).to(device)
            cond_img_autoregressive = torch.cat([auto_reg[0] for auto_reg in autoregressive_list], dim=0)[valid_index].to(device)

            rotation_cond_tmp[:, 1] = torch.sin(torch.from_numpy(rotation_cond_angle))
            rotation_cond_tmp[:, 2] = torch.cos(torch.from_numpy(rotation_cond_angle))
            rotation_cond_tmp = rotation_cond_tmp.to(device)[valid_index]

            x_samples_ddim_tmp = sample_model(cond_img_autoregressive, models['turncam'], sampler, precision, ddim_steps, scale, ddim_eta, rotation_cond_tmp, interpolation_weight=interpolation_weight)
            
            autoregressive_list.append((x_samples_ddim_tmp.cpu(), view_angle))
            
            torch.cuda.empty_cache()
        
        autoregressive_list = sorted(autoregressive_list, key=lambda x: x[1]%(2*np.pi))
        x_samples_ddim = torch.cat([auto_reg[0] for auto_reg in autoregressive_list], dim=0)
        x_samples_ddim = 255.0 * rearrange(x_samples_ddim.cpu().numpy()/2+0.5, 'b c h w ->b h w c')

        for i in range(x_samples_ddim.shape[0]):
            x_sample = x_samples_ddim[i]
            # output_ims.append(Image.fromarray(x_sample.astype(np.uint8)))
            img = Image.fromarray(x_sample.astype(np.uint8))
            output_path = f"{output_dir}/{model_name[i//degree_steps]}/{env_index[i//degree_steps]}/{i%degree_steps}.png"
            if not os.path.exists(f"{output_dir}/{model_name[i//degree_steps]}/{env_index[i//degree_steps]}/"):
                os.makedirs(f"{output_dir}/{model_name[i//degree_steps]}/{env_index[i//degree_steps]}/")
            img.save(output_path)
        inference = 255.0 * rearrange(inference.cpu().numpy()/2+0.5, 'b c h w ->b h w c')
        Image.fromarray(inference[0].astype(np.uint8)).save(f"{output_dir}/{model_name[0]}/{env_index[0]}/condition.png")
        torch.cuda.empty_cache()
    return

if __name__ == '__main__':
    random_seed = 8007 # 3407
    random.seed(random_seed)
    np.random.seed(random_seed)
    torch.manual_seed(random_seed)
    mp.set_start_method('spawn')
    torch.set_num_threads(1)

    import argparse
    parser = argparse.ArgumentParser(description='Process some integers.')
    parser.add_argument('--inference_temp', default=0.5, help='inference temperature', type=float)
    parser.add_argument('--auto_temp', default=0.5, help='autoregressive temperature', type=float)
    args = parser.parse_args()

    device_idx=_GPU_INDEX
    ckpt='./zero123/zero123-xl.ckpt'
    config='./configs/sd-swap_att-c_concat-256.yaml'

    config = OmegaConf.load(config)

    # Instantiate all models beforehand for efficiency.
    device = f'cuda:{device_idx}'
    models = dict()
    print('Instantiating LatentDiffusion...')
    models['turncam'] = load_model_from_config(config, ckpt, device=device)

    dataset = RealDataset()

    inference_temp = args.inference_temp
    auto_temp = args.auto_temp
    output_dir = f'./experiments_cvpr/pretrain_zero123_xl_360_autoregressive/gen_inference_t{inference_temp:.2f}_auto_t{auto_temp:.2f}'
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    with open(f"{output_dir}/parameter.txt", "w") as f:
        f.write(f"inference_temp: {inference_temp}\n")
        f.write(f"auto_temp: {auto_temp}\n")
        
    main_run(models, device, dataset, inference_temp, auto_temp, output_dir)
