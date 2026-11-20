"""Fine-tune the Refined segmenter on an annotated COCO training split."""

import argparse
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from torch.utils.data import DataLoader, Dataset
from torchvision.models import mobilenet_v2

from data import Coco, letterbox, normalize, targets

ROOT = Path(__file__).resolve().parent


class Segmenter(nn.Module):
    def __init__(self, widths=(96, 64, 32, 24)):
        super().__init__()
        self.encoder = mobilenet_v2(weights=None).features[:18]
        self.indices = (1, 3, 6, 13, 17)
        self.decoder = nn.ModuleList(
            nn.Sequential(nn.Conv2d(prev + skip, width, 3, padding=1, bias=False),
                          nn.BatchNorm2d(width), nn.ReLU(inplace=True))
            for prev, skip, width in zip((320, *widths), (96, 32, 24, 16), widths)
        )
        self.head = nn.Conv2d(widths[-1], 2, 1)

    def forward(self, x):
        skips = []
        for index, layer in enumerate(self.encoder):
            x = layer(x)
            if index in self.indices:
                skips.append(x)
        for skip, layer in zip(reversed(skips[:-1]), self.decoder):
            x = F.interpolate(x, scale_factor=2, mode="bilinear", align_corners=False)
            x = layer(torch.cat((x, skip), dim=1))
        return F.interpolate(self.head(x), scale_factor=2, mode="bilinear", align_corners=False)


class TrainingImages(Dataset):
    def __init__(self, root, size):
        self.source = Coco(root, "train")
        self.size = size

    def __len__(self):
        return len(self.source.images)

    def __getitem__(self, index):
        image, instances = self.source.load(index)
        image, box = letterbox(image, self.size)
        mask, valid = targets(instances, box, self.size)
        return normalize(image), np.concatenate((mask, valid))


def segmentation_loss(logits, target, valid):
    axes = (0, 2, 3)
    probability = logits.sigmoid() * valid
    bce = F.binary_cross_entropy_with_logits(logits, target, reduction="none") * valid
    dice = 1 - (2 * (probability * target).sum(axes) + 1) / (
        (probability + target * valid).sum(axes) + 1
    )
    return ((bce.sum(axes) / valid.sum() + dice) * logits.new_tensor([1, 1.6])).mean()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path, help="Directory with train/_annotations.coco.json")
    parser.add_argument("--out", required=True, type=Path, help="Directory for new training outputs")
    parser.add_argument("--init", type=Path, default=ROOT / "weights" / "best.pt")
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = parser.parse_args()
    if args.epochs < 1 or args.batch < 1:
        parser.error("--epochs and --batch must be positive")
    output = args.out / "last.pt"
    if output.resolve() == args.init.resolve() or output.exists():
        parser.error("Use a new output directory for training")

    state = torch.load(args.init, map_location="cpu", weights_only=True)
    config = state["config"]
    model = Segmenter(config["widths"])
    model.load_state_dict(state["model"])
    model.to(args.device)
    optimizer = torch.optim.AdamW([
        {"params": model.encoder.parameters(), "lr": 3.5e-5},
        {"params": [*model.decoder.parameters(), *model.head.parameters()], "lr": 7.5e-5},
    ])
    images = TrainingImages(args.data_root, config["size"])
    batches = DataLoader(images, batch_size=args.batch, shuffle=True)
    args.out.mkdir(parents=True, exist_ok=True)
    for epoch in range(1, args.epochs + 1):
        model.train()
        total = 0.0
        for image, labels in batches:
            image, labels = image.to(args.device), labels.to(args.device)
            rotation = int(torch.randint(4, ()).item())
            image, labels = (torch.rot90(value, rotation, (-2, -1)) for value in (image, labels))
            if torch.rand(()).item() < 0.5:
                image, labels = image.flip(-1), labels.flip(-1)
            loss = segmentation_loss(model(image), labels[:, :2], labels[:, 2:])
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 2)
            optimizer.step()
            total += float(loss.detach())
        print(f"Epoch {epoch}/{args.epochs} loss {total / len(batches):.5f}", flush=True)
        temporary = output.with_suffix(".tmp")
        torch.save({"model": model.state_dict(), "config": config, "epoch": epoch}, temporary)
        temporary.replace(output)


if __name__ == "__main__":
    main()
