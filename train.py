#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import os
import torch
from pytorch_wavelets import DWTForward, DWTInverse
import matplotlib.pyplot as plt
import numpy as np
from random import randint
from utils.loss_utils import l1_loss, ssim
from gaussian_renderer import render, network_gui
import sys
from scene import Scene, GaussianModel
from utils.general_utils import safe_state, get_expon_lr_func
import uuid
from tqdm import tqdm
from utils.image_utils import psnr
from argparse import ArgumentParser, Namespace
from arguments import ModelParams, PipelineParams, OptimizationParams
try:
    from torch.utils.tensorboard import SummaryWriter
    TENSORBOARD_FOUND = True
except ImportError:
    TENSORBOARD_FOUND = False

try:
    from fused_ssim import fused_ssim
    FUSED_SSIM_AVAILABLE = True
except:
    FUSED_SSIM_AVAILABLE = False

try:
    from diff_gaussian_rasterization import SparseGaussianAdam
    SPARSE_ADAM_AVAILABLE = True
except:
    SPARSE_ADAM_AVAILABLE = False

def training(dataset, opt, pipe, testing_iterations, saving_iterations, checkpoint_iterations, checkpoint, debug_from):

    if not SPARSE_ADAM_AVAILABLE and opt.optimizer_type == "sparse_adam":
        sys.exit(f"Trying to use sparse adam but it is not installed, please install the correct rasterizer using pip install [3dgs_accel].")

    first_iter = 0
    tb_writer = prepare_output_and_logger(dataset)
    gaussians = GaussianModel(dataset.sh_degree, opt.optimizer_type)
    scene = Scene(dataset, gaussians)
    gaussians.training_setup(opt)
    if checkpoint:
        (model_params, first_iter) = torch.load(checkpoint)
        gaussians.restore(model_params, opt)

    bg_color = [1, 1, 1] if dataset.white_background else [0, 0, 0]
    background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")

    iter_start = torch.cuda.Event(enable_timing = True)
    iter_end = torch.cuda.Event(enable_timing = True)

    use_sparse_adam = opt.optimizer_type == "sparse_adam" and SPARSE_ADAM_AVAILABLE 
    depth_l1_weight = get_expon_lr_func(opt.depth_l1_weight_init, opt.depth_l1_weight_final, max_steps=opt.iterations)

    viewpoint_stack = scene.getTrainCameras().copy()
    viewpoint_indices = list(range(len(viewpoint_stack)))
    ema_loss_for_log = 0.0
    ema_Ll1depth_for_log = 0.0

    progress_bar = tqdm(range(first_iter, opt.iterations), desc="Training progress")
    first_iter += 1
    for iteration in range(first_iter, opt.iterations + 1):
        if network_gui.conn == None:
            network_gui.try_connect()
        while network_gui.conn != None:
            try:
                net_image_bytes = None
                custom_cam, do_training, pipe.convert_SHs_python, pipe.compute_cov3D_python, keep_alive, scaling_modifer = network_gui.receive()
                if custom_cam != None:
                    net_image = render(custom_cam, gaussians, pipe, background, scaling_modifier=scaling_modifer, use_trained_exp=dataset.train_test_exp, separate_sh=SPARSE_ADAM_AVAILABLE)["render"]
                    net_image_bytes = memoryview((torch.clamp(net_image, min=0, max=1.0) * 255).byte().permute(1, 2, 0).contiguous().cpu().numpy())
                network_gui.send(net_image_bytes, dataset.source_path)
                if do_training and ((iteration < int(opt.iterations)) or not keep_alive):
                    break
            except Exception as e:
                network_gui.conn = None

        iter_start.record()

        gaussians.update_learning_rate(iteration)

        # Every 1000 its we increase the levels of SH up to a maximum degree
        if iteration % 1000 == 0:
            gaussians.oneupSHdegree()

        # Pick a random Camera
        if not viewpoint_stack:
            viewpoint_stack = scene.getTrainCameras().copy()
            viewpoint_indices = list(range(len(viewpoint_stack)))
        rand_idx = randint(0, len(viewpoint_indices) - 1)
        viewpoint_cam = viewpoint_stack.pop(rand_idx)
        vind = viewpoint_indices.pop(rand_idx)

        # Render
        if (iteration - 1) == debug_from:
            pipe.debug = True

        bg = torch.rand((3), device="cuda") if opt.random_background else background

        render_pkg = render(viewpoint_cam, gaussians, pipe, bg, use_trained_exp=dataset.train_test_exp, separate_sh=SPARSE_ADAM_AVAILABLE)
        image, viewspace_point_tensor, visibility_filter, radii, pixel_to_gaussians = render_pkg["render"], render_pkg["viewspace_points"], render_pkg["visibility_filter"], render_pkg["radii"], render_pkg["pixel_to_gaussians"]

        if viewpoint_cam.alpha_mask is not None:
            alpha_mask = viewpoint_cam.alpha_mask.cuda()
            image *= alpha_mask

        # Loss
        gt_image = viewpoint_cam.original_image.cuda()

        Ll1 = l1_loss(image, gt_image)
        if FUSED_SSIM_AVAILABLE:
            ssim_value = fused_ssim(image.unsqueeze(0), gt_image.unsqueeze(0))
        else:
            ssim_value = ssim(image, gt_image)

        wavelet_weights = [0.0, 0.0, 15.0]

        if iteration > opt.densify_from_iter and iteration < opt.densify_until_iter:
            # Progressive phase: gradually increase high-frequency weights
            progress = (iteration - opt.densify_from_iter) / (opt.densify_until_iter - opt.densify_from_iter)
            w_l1 = progress * 20.0
            w_l2 = progress * 50.0
            wavelet_weights = [w_l2, w_l1, 15.0]
        elif iteration > opt.densify_until_iter:
            wavelet_weights = [0.0, 0.0, 0.0]

        decomp_levels = 3      
        wavelet = 'sym4'
        dwt = DWTForward(J=decomp_levels, wave=wavelet).cuda()
        ifm = DWTInverse(wave=wavelet).cuda()

        coeffs_rendered = dwt(image.unsqueeze(0)) # (N, C, levels, H, W)
        coeffs_gt = dwt(gt_image.unsqueeze(0))    # (N, C, levels, H, W)

        wavelet_loss = 0.0
        _, highpass_gt = coeffs_gt
        _, highpass_rend = coeffs_rendered

        for level in range(decomp_levels):

            residual_h = torch.abs(highpass_gt[level][:, :, 0] - highpass_rend[level][:, :, 0])  # LH (horizontal)
            residual_v = torch.abs(highpass_gt[level][:, :, 1] - highpass_rend[level][:, :, 1])  # HL (vertical)
            residual_d = torch.abs(highpass_gt[level][:, :, 2] - highpass_rend[level][:, :, 2])  # HH (diagonal)

            num_coeffs = residual_d.numel()
            norm_factor = 1.0 / torch.sqrt(torch.tensor(num_coeffs, dtype=torch.float32))

            # Aggregate across channels
            residual_h = residual_h.mean()
            residual_v = residual_v.mean()
            residual_d = residual_d.mean()

            level_loss = (residual_h.sum() + residual_v.sum() + residual_d.sum()) * norm_factor

            # Compute mean of top-k values
            wavelet_loss += level_loss * wavelet_weights[level]

        gaussians_to_densify = None

        if iteration >= opt.densify_from_iter and iteration <= opt.densify_until_iter:
            # Compute discrepancies in wavelet coefficients
            discrepancy_coeffs = []
            for gt_c, rend_c in zip(coeffs_gt[1], coeffs_rendered[1]):  # Iterate over levels
                # Compute absolute differences for each high-frequency sub-band
                discrepancy_level = torch.abs(gt_c - rend_c)  # Shape: [N, C, 3, H, W]
                discrepancy_coeffs.append(discrepancy_level)

            # Use zeros for the approximation coefficients during reconstruction
            approx_zeros = torch.zeros_like(coeffs_gt[0])

            # Reconstruct discrepancies back to pixel space
            reconstructed_discrepancy = ifm((approx_zeros, discrepancy_coeffs))

            # Sum across color channels to get per-pixel discrepancy
            per_pixel_discrepancy = reconstructed_discrepancy.sum(dim=1) # > wavelet_threshold  # [B, H, W]

            _, height, width = gt_image.shape

            per_pixel_discrepancy = per_pixel_discrepancy[:,:height,:width]

            B, H, W = per_pixel_discrepancy.shape
            pixel_to_gaussians = pixel_to_gaussians[:,:,:3]
            K = pixel_to_gaussians.shape[2]
            if iteration == 500:
                print(f"***buffer dim: {K}***")

            d_max = per_pixel_discrepancy.max()
            d_min = per_pixel_discrepancy.min()

            normalized_tensor = (per_pixel_discrepancy - d_min) / (d_max - d_min)
            
            percentile = 0.999

            # if 5000 <= iteration <= 7000:
            #     percentile = 0.995
            # if 7001 <= iteration <= 9000:
            #     percentile = 0.25
            if 7001 <= iteration <= opt.densify_until_iter:
                percentile = 0.995

            cutoff = torch.quantile(normalized_tensor.flatten(), percentile)
            discrepancy_mask = per_pixel_discrepancy > cutoff 

            # discrepancy_mask = normalized_tensor > wavelet_threshold

            # Flatten the masks and indices
            high_discrepancy_mask_flat = discrepancy_mask.contiguous().view(-1)  # [B*H*W]
            pixel_to_gaussians_flat = pixel_to_gaussians.view(-1, K)     # [B*H*W, K]

            # Filter indices where the pixel has high discrepancy
            selected_gaussians = pixel_to_gaussians_flat[high_discrepancy_mask_flat]  # [N, K]

            # Remove invalid indices (-1)
            valid_gaussians = selected_gaussians[selected_gaussians >= 0]

            # Get unique Gaussian indices
            gaussians_to_densify = torch.unique(valid_gaussians.long())

            if iteration % 500 == 0:
                ratio = gaussians_to_densify.shape[0] / gaussians.get_xyz.shape[0]
                print(f"Affected gaussians: {ratio*100:.4f}%")
                print(f"wavelet cutoff:{cutoff:.4f}")
                plt.figure(figsize=(10, 10))  # Adjust the figsize to control the image size

                # Plot the discrepancy mask
                plt.imshow(discrepancy_mask.view(height, width).cpu().numpy(), cmap='hot', interpolation='nearest')
                plt.colorbar(label="Discrepancy Intensity")
                plt.title(f"Significant Discrepancies - Iteration {iteration}")

                # Save the figure
                plt.savefig(f"iter-{iteration}.png", dpi=300)  # Save with a high resolution if needed

                # Close the figure to prevent overlap in subsequent iterations
                plt.close()
                print("\nNumber of gaussians: {}".format(gaussians.get_xyz.shape[0]))

        L1_LOSS = (1.0 - opt.lambda_dssim) * Ll1
        SSIM_LOSS = opt.lambda_dssim * (1.0 - ssim_value)
        
        loss = L1_LOSS + SSIM_LOSS + wavelet_loss
        if iteration % 500 == 0:
            L1_contribution = L1_LOSS.item()
            SSIM_contribution = SSIM_LOSS.item()
            Wavelet_contribution = wavelet_loss.item()

            print(f"L1 Loss Contribution: {L1_contribution:.6f}")
            print(f"SSIM Contribution: {SSIM_contribution:.6f}")
            print(f"Wavelet Regularization Contribution: {Wavelet_contribution:.6f}")
            print(f"Total Loss: {loss.item():.6f}")

            total_contribution = L1_contribution + SSIM_contribution + Wavelet_contribution
            L1_ratio = L1_contribution / total_contribution
            SSIM_ratio = SSIM_contribution / total_contribution
            Wavelet_ratio = Wavelet_contribution / total_contribution

            print(f"Relative Contributions: L1 = {L1_ratio:.2%}, SSIM = {SSIM_ratio:.2%}, Wavelet = {Wavelet_ratio:.2%}")

            gaussians.optimizer.zero_grad()       
                
            L1_LOSS.backward(retain_graph=True)
            L1_grad_xyz = gaussians._xyz.grad.norm().item()
            print(f"L1 Gradient Magnitude (XYZ): {L1_grad_xyz:.6f}")

            gaussians.optimizer.zero_grad()       
                
            SSIM_LOSS.backward(retain_graph=True)
            SSIM_grad_xyz = gaussians._xyz.grad.norm().item()
            print(f"SSIM Gradient Magnitude (XYZ): {SSIM_grad_xyz:.6f}")

            gaussians.optimizer.zero_grad()       
                
            wavelet_loss.backward(retain_graph=True)
            wavelet_grad_xyz = gaussians._xyz.grad.norm().item()
            print(f"Wavelet Gradient Magnitude (XYZ): {wavelet_grad_xyz:.6f}")
            print("--------------------------------------------")
            

        # Depth regularization
        Ll1depth_pure = 0.0
        if depth_l1_weight(iteration) > 0 and viewpoint_cam.depth_reliable:
            invDepth = render_pkg["depth"]
            mono_invdepth = viewpoint_cam.invdepthmap.cuda()
            depth_mask = viewpoint_cam.depth_mask.cuda()

            Ll1depth_pure = torch.abs((invDepth  - mono_invdepth) * depth_mask).mean()
            Ll1depth = depth_l1_weight(iteration) * Ll1depth_pure 
            loss += Ll1depth
            Ll1depth = Ll1depth.item()
        else:
            Ll1depth = 0

        loss.backward()

        iter_end.record()

        with torch.no_grad():
            # Progress bar
            ema_loss_for_log = 0.4 * loss.item() + 0.6 * ema_loss_for_log
            ema_Ll1depth_for_log = 0.4 * Ll1depth + 0.6 * ema_Ll1depth_for_log

            if iteration % 10 == 0:
                progress_bar.set_postfix({"Loss": f"{ema_loss_for_log:.{7}f}", "Depth Loss": f"{ema_Ll1depth_for_log:.{7}f}"})
                progress_bar.update(10)
            if iteration == opt.iterations:
                progress_bar.close()

            # Log and save
            training_report(tb_writer, iteration, Ll1, loss, l1_loss, iter_start.elapsed_time(iter_end), testing_iterations, scene, render, (pipe, background, 1., SPARSE_ADAM_AVAILABLE, None, dataset.train_test_exp), dataset.train_test_exp)
            if (iteration in saving_iterations):
                print("\n[ITER {}] Saving Gaussians".format(iteration))
                print("\nNumber of gaussians: {}".format(gaussians.get_xyz.shape[0]))
                scene.save(iteration)
            if iteration == 15000:
                print("\nNumber of gaussians: {}".format(gaussians.get_xyz.shape[0]))

            # Densification
            if iteration < opt.densify_until_iter:
                # Keep track of max radii in image-space for pruning
                gaussians.max_radii2D[visibility_filter] = torch.max(gaussians.max_radii2D[visibility_filter], radii[visibility_filter])
                gaussians.add_densification_stats(viewspace_point_tensor, visibility_filter)

                if iteration > opt.densify_from_iter and iteration % opt.densification_interval == 0:
                    size_threshold = 20 if iteration > opt.opacity_reset_interval else None
                    gaussians.densify_and_prune(opt.densify_grad_threshold, 0.02, scene.cameras_extent, size_threshold, radii, gaussians_to_densify)
                
                if iteration % opt.opacity_reset_interval == 0 or (dataset.white_background and iteration == opt.densify_from_iter):
                    gaussians.reset_opacity()

            # Optimizer step
            if iteration < opt.iterations:
                gaussians.exposure_optimizer.step()
                gaussians.exposure_optimizer.zero_grad(set_to_none = True)
                if use_sparse_adam:
                    visible = radii > 0
                    gaussians.optimizer.step(visible, radii.shape[0])
                    gaussians.optimizer.zero_grad(set_to_none = True)
                else:
                    gaussians.optimizer.step()
                    gaussians.optimizer.zero_grad(set_to_none = True)

            if (iteration in checkpoint_iterations):
                print("\n[ITER {}] Saving Checkpoint".format(iteration))
                torch.save((gaussians.capture(), iteration), scene.model_path + "/chkpnt" + str(iteration) + ".pth")

def prepare_output_and_logger(args):    
    if not args.model_path:
        if os.getenv('OAR_JOB_ID'):
            unique_str=os.getenv('OAR_JOB_ID')
        else:
            unique_str = str(uuid.uuid4())
        args.model_path = os.path.join("./output/", unique_str[0:10])
        
    # Set up output folder
    print("Output folder: {}".format(args.model_path))
    os.makedirs(args.model_path, exist_ok = True)
    with open(os.path.join(args.model_path, "cfg_args"), 'w') as cfg_log_f:
        cfg_log_f.write(str(Namespace(**vars(args))))

    # Create Tensorboard writer
    tb_writer = None
    if TENSORBOARD_FOUND:
        tb_writer = SummaryWriter(args.model_path)
    else:
        print("Tensorboard not available: not logging progress")
    return tb_writer

def training_report(tb_writer, iteration, Ll1, loss, l1_loss, elapsed, testing_iterations, scene : Scene, renderFunc, renderArgs, train_test_exp):
    if tb_writer:
        tb_writer.add_scalar('train_loss_patches/l1_loss', Ll1.item(), iteration)
        tb_writer.add_scalar('train_loss_patches/total_loss', loss.item(), iteration)
        tb_writer.add_scalar('iter_time', elapsed, iteration)

    # Report test and samples of training set
    if iteration in testing_iterations:
        torch.cuda.empty_cache()
        validation_configs = ({'name': 'test', 'cameras' : scene.getTestCameras()}, 
                              {'name': 'train', 'cameras' : [scene.getTrainCameras()[idx % len(scene.getTrainCameras())] for idx in range(5, 30, 5)]})

        for config in validation_configs:
            if config['cameras'] and len(config['cameras']) > 0:
                l1_test = 0.0
                psnr_test = 0.0
                for idx, viewpoint in enumerate(config['cameras']):
                    image = torch.clamp(renderFunc(viewpoint, scene.gaussians, *renderArgs)["render"], 0.0, 1.0)
                    gt_image = torch.clamp(viewpoint.original_image.to("cuda"), 0.0, 1.0)
                    if train_test_exp:
                        image = image[..., image.shape[-1] // 2:]
                        gt_image = gt_image[..., gt_image.shape[-1] // 2:]
                    if tb_writer and (idx < 5):
                        tb_writer.add_images(config['name'] + "_view_{}/render".format(viewpoint.image_name), image[None], global_step=iteration)
                        if iteration == testing_iterations[0]:
                            tb_writer.add_images(config['name'] + "_view_{}/ground_truth".format(viewpoint.image_name), gt_image[None], global_step=iteration)
                    l1_test += l1_loss(image, gt_image).mean().double()
                    psnr_test += psnr(image, gt_image).mean().double()
                psnr_test /= len(config['cameras'])
                l1_test /= len(config['cameras'])          
                print("\n[ITER {}] Evaluating {}: L1 {} PSNR {}".format(iteration, config['name'], l1_test, psnr_test))
                if tb_writer:
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - l1_loss', l1_test, iteration)
                    tb_writer.add_scalar(config['name'] + '/loss_viewpoint - psnr', psnr_test, iteration)

        if tb_writer:
            tb_writer.add_histogram("scene/opacity_histogram", scene.gaussians.get_opacity, iteration)
            tb_writer.add_scalar('total_points', scene.gaussians.get_xyz.shape[0], iteration)
        torch.cuda.empty_cache()

if __name__ == "__main__":
    # Set up command line argument parser
    parser = ArgumentParser(description="Training script parameters")
    lp = ModelParams(parser)
    op = OptimizationParams(parser)
    pp = PipelineParams(parser)
    parser.add_argument('--ip', type=str, default="127.0.0.1")
    parser.add_argument('--port', type=int, default=6009)
    parser.add_argument('--debug_from', type=int, default=-1)
    parser.add_argument('--detect_anomaly', action='store_true', default=False)
    parser.add_argument("--test_iterations", nargs="+", type=int, default=[7_000, 30_000])
    parser.add_argument("--save_iterations", nargs="+", type=int, default=[7_000, 30_000])
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument('--disable_viewer', action='store_true', default=False)
    parser.add_argument("--checkpoint_iterations", nargs="+", type=int, default=[])
    parser.add_argument("--start_checkpoint", type=str, default = None)
    args = parser.parse_args(sys.argv[1:])
    args.save_iterations.append(args.iterations)
    
    print("Optimizing " + args.model_path)

    # Initialize system state (RNG)
    safe_state(args.quiet)

    # Start GUI server, configure and run training
    if not args.disable_viewer:
        network_gui.init(args.ip, args.port)
    torch.autograd.set_detect_anomaly(args.detect_anomaly)
    training(lp.extract(args), op.extract(args), pp.extract(args), args.test_iterations, args.save_iterations, args.checkpoint_iterations, args.start_checkpoint, args.debug_from)

    # All done
    print("\nTraining complete.")
