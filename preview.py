"""Class-colored previews for Day 3 instance segmentation."""
import cv2
import numpy as np
from scipy.ndimage import find_objects


# RGB values from the swatches on the Day 3 reference slide.
CLASS_COLORS = {
    "husk": (160, 90, 40),
    "parchment": (240, 200, 90),
    "immature": (120, 200, 120),
    "good": (160, 60, 170),
    "shell": (60, 160, 200),
    "black": (40, 40, 40),
    "broken": (220, 60, 60),
}


def class_overlay(rgb, labels, objects, max_side=1280):
    """Return a BGR preview for OpenCV image encoding."""
    if 0 < max_side < max(labels.shape):
        scale = max_side / max(labels.shape)
        size = (max(1, round(labels.shape[1] * scale)), max(1, round(labels.shape[0] * scale)))
        rgb = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)
        labels = cv2.resize(labels, size, interpolation=cv2.INTER_NEAREST)
    preview = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    colors = {obj["id"]: CLASS_COLORS[obj["class"]][::-1] for obj in objects}
    for object_id, box in enumerate(find_objects(labels), 1):
        if box is None:
            continue
        mask = labels[box] == object_id
        patch = preview[box]
        color = colors[object_id]
        patch[mask] = np.rint(0.65 * patch[mask] + 0.35 * np.asarray(color)).astype(np.uint8)
        contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(patch, contours, -1, color, 1, cv2.LINE_AA)
    return preview
