# Copyright (c) Meta Platforms, Inc. and affiliates.

import torch
from tqdm import tqdm

from utils.gen_utils import AverageMeter
from utils.dist_utils import all_gather_batch_with_grad, gather_meter_vals
from utils.metric_utils import get_recall

# -----------------
# Train function
# -----------------
def train_one_epoch(clip_model, combiner, trainloader, optimizer, args):

    # For now...
    assert args.base_contrastive == 1

    if args.finetune_mode == None:
        print(f'Training with backbone in eval mode...')
        clip_model.eval()
    else:
        clip_model.train()

    combiner.train()
    clip_trainable_params = [p for p in clip_model.parameters() if p.requires_grad]

    # Get average meters
    # Loss
    loss_meters = {}
    loss_meters['base_loss'] = AverageMeter()

    # Accuracy
    loss_meters['base_acc'] = AverageMeter()

    # Other metrics
    loss_meters['combiner_text_feat_dist'] = AverageMeter()
    loss_meters['combiner_image_feat_dist'] = AverageMeter()


    for batch_idx, batch in enumerate(tqdm(trainloader)):

        torch.cuda.empty_cache()

        # Batch to GPU
        ref_imgs, target_imgs, text_distractor_imgs, captions = [x.cuda(non_blocking=True) for x in batch]
        captions = captions.squeeze()

        with torch.autocast(device_type='cuda', dtype=torch.float16):
            
            # Forward pass of text and images
            imgs_ = torch.cat([ref_imgs, target_imgs, text_distractor_imgs], dim=0)
            ref_feats, target_feats, text_distractor_feats = clip_model.encode_image(imgs_).chunk(3)
            caption_feats = clip_model.encode_text(captions)

            # Pass reference features and captions into combiner
            combined_feats = combiner(ref_feats, caption_feats)

            # L2-normalize
            combined_feats = torch.nn.functional.normalize(combined_feats, p=2, dim=-1)     # B x D
            target_feats = torch.nn.functional.normalize(target_feats, p=2, dim=-1)       # B x D
            text_distractor_feats = torch.nn.functional.normalize(text_distractor_feats, p=2, dim=-1)       # B x D

            # Gather features from all processes if distributed
            combined_feats, target_feats, ref_feats, caption_feats, text_distractor_feats = all_gather_batch_with_grad([combined_feats, target_feats, ref_feats, caption_feats, text_distractor_feats])

            # Contrastive loss
            logits = args.lamda * torch.matmul(combined_feats, target_feats.t())     # B x B
            targets = torch.arange(0, logits.size(0)).cuda()                   # B,
            total_loss = torch.nn.CrossEntropyLoss()(logits, targets)
            
            # Record base loss and acc
            train_acc = (logits.argmax(dim=-1) == targets).float().mean().item()
            loss_meters['base_loss'].update(total_loss.item(), n=ref_feats.size(0))
            loss_meters['base_acc'].update(train_acc, n=ref_feats.size(0))

            # Record distance between combined feats and text and image
            with torch.no_grad():
                
                ref_feats_norm = torch.nn.functional.normalize(ref_feats, dim=-1)
                caption_feats_norm = torch.nn.functional.normalize(caption_feats, dim=-1)

                img_combiner_dist = torch.matmul(ref_feats_norm, combined_feats.t())
                text_combiner_dist = torch.matmul(caption_feats_norm, combined_feats.t())

                loss_meters['combiner_text_feat_dist'].update(text_combiner_dist.mean().item(), logits.size(0))
                loss_meters['combiner_image_feat_dist'].update(img_combiner_dist.mean().item(), logits.size(0))

        # Optimizer step
        optimizer.zero_grad()
        args.scaler.scale(total_loss).backward()

        if args.clip_grad_norm:
            torch.nn.utils.clip_grad_norm_(clip_trainable_params, 1.0)
    
        args.scaler.step(optimizer)
        args.scaler.update()

    # Collect loss metrics and return
    return {loss_name: gather_meter_vals(meter) for loss_name, meter in loss_meters.items()}

# -----------------
# Val function
# -----------------
@torch.no_grad()
def val_one_epoch(clip_model, combiner, valloader, args, recall_topk=(1, 5, 10)):

    """
    Validation function similar to train function. Returns loss and acc on a val set
    """

    clip_model.eval()
    combiner.eval()

    # Instantiate average meters
    loss_meters = {}
    loss_meters['base_loss'] = AverageMeter()
    loss_meters['base_acc'] = AverageMeter()

    # Recall meters
    for k in recall_topk:
        loss_meters[f'Recall @ {k}'] = AverageMeter()

    for batch_idx, batch in enumerate(tqdm(valloader)):

        # Batch to GPU
        ref_imgs, target_imgs, text_distractor_imgs, captions = [x.cuda(non_blocking=True) for x in batch]
        captions = captions.squeeze()

        with torch.autocast(device_type='cuda', dtype=torch.float16):
            
            # Forward pass of text and images
            imgs_ = torch.cat([ref_imgs, target_imgs, text_distractor_imgs], dim=0)
            ref_feats, target_feats, text_distractor_feats = clip_model.encode_image(imgs_).chunk(3)
            caption_feats = clip_model.encode_text(captions)

            # Pass reference features and captions into combiner
            combined_feats = combiner(ref_feats, caption_feats)

            # L2-normalize
            combined_feats = torch.nn.functional.normalize(combined_feats, p=2, dim=-1)     # B x D
            target_feats = torch.nn.functional.normalize(target_feats, p=2, dim=-1)       # B x D
            text_distractor_feats = torch.nn.functional.normalize(text_distractor_feats, p=2, dim=-1)       # B x D

            # Gather features from all processes if distributed
            combined_feats, target_feats, ref_feats, caption_feats, text_distractor_feats = all_gather_batch_with_grad([combined_feats, target_feats, ref_feats, caption_feats, text_distractor_feats])

            # Contrastive loss
            logits = args.lamda * torch.matmul(combined_feats, target_feats.t())     # B x B
            targets = torch.arange(0, logits.size(0)).cuda()                   # B,
            total_loss = torch.nn.CrossEntropyLoss()(logits, targets)

            # Record base loss and acc
            val_acc = (logits.argmax(dim=-1) == targets).float().mean().item()
            loss_meters['base_loss'].update(total_loss.item(), n=ref_feats.size(0))
            loss_meters['base_acc'].update(val_acc, n=ref_feats.size(0))

            # Record retrieval performance
            # Sort the similarities in ascending order (closest example is the predicted sample)
            _, sort_idxs = logits.sort(dim=-1, descending=True)                   # B x N
            # Compute recall at K
            for k in recall_topk:

                recall_k = get_recall(sort_idxs[:, :k], targets)
                loss_meters[f'Recall @ {k}'].update(recall_k, ref_feats.size(0))

    # Collect loss metrics and return
    return {loss_name: gather_meter_vals(meter) for loss_name, meter in loss_meters.items()}
