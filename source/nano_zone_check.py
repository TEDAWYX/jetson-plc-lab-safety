#!/usr/bin/env python3
"""Monitor a taped floor area from a fixed demo camera using an empty reference."""

import math
from statistics import median
import sys
import time

from nano_bottle_check import HEIGHT, WIDTH, UNKNOWN, components, main


def is_tape(red, green, blue):
    return red >= 80 and red - green >= 22 and red - blue >= 22


def cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def tape_groups(pixels, spacing):
    if spacing == 1:
        return components(set(pixels), 200, WIDTH * HEIGHT)
    # Connect nearby fragments on a coarse grid, but fit only real tape pixels.
    cells = {}
    for x, y in pixels:
        cells.setdefault((x // spacing, y // spacing), []).append((x, y))
    return ([point for cell in group for point in cells[cell]]
            for group in components(set(cells), 4, len(cells)))


def find_outline(pixels):
    failure = "red tape outline was not found"
    cropped = False
    for spacing in (1, 8, 16, 24):
        groups = sorted(tape_groups(pixels, spacing), key=len, reverse=True)
        for tape in groups[:3]:
            if len(tape) < 200:
                continue
            if any(x <= 3 or y <= 3 or x >= WIDTH - 4 or y >= HEIGHT - 4 for x, y in tape):
                cropped = True
                continue
            try:
                return quadrilateral(tape), tape
            except ValueError as error:
                failure = str(error)
    if cropped:
        failure = "part of the red outline touches the image edge; move the camera back"
    raise ValueError(failure)


def quadrilateral(points):
    ordered = sorted(points)
    hull = []
    for sequence in (ordered, reversed(ordered)):
        half = []
        for point in sequence:
            while len(half) >= 2 and cross(half[-2], half[-1], point) <= 0:
                half.pop()
            half.append(point)
        hull.extend(half[:-1])

    # Simplify the hull, then fit the centerline of each tape edge.
    while len(hull) > 4:
        least = min(range(len(hull)), key=lambda i: cross(hull[i - 1], hull[i], hull[(i + 1) % len(hull)]))
        hull.pop(least)
    if len(hull) != 4:
        raise ValueError("could not find four corners")
    sides = [fit_edge(a, b, points) for a, b in zip(hull, hull[1:] + hull[:1])]
    corners = [intersection(sides[i - 1], sides[i]) for i in range(4)]
    if not all(3 < x < WIDTH - 4 and 3 < y < HEIGHT - 4 for x, y in corners):
        raise ValueError("one of the corners is near or outside the image edge")
    if min(cross(corners[i - 1], corners[i], corners[(i + 1) % 4]) for i in range(4)) < 1800:
        raise ValueError("the zone is too small or has an unclear shape")
    return corners


def fit_edge(a, b, points):
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    samples = []
    for x, y in points:
        dx, dy = x - a[0], y - a[1]
        along, across = dx * ux + dy * uy, -dx * uy + dy * ux
        if 0.14 * length < along < 0.86 * length and abs(across) < 17:
            samples.append((along, across))
    n = len(samples)
    if n < max(14, 0.27 * length):
        raise ValueError("one edge of the tape is incomplete")
    positions = [t for t, _ in samples]
    if max(positions) - min(positions) < 0.24 * length:
        raise ValueError("one edge is only visible in a small patch")
    mean_t = sum(t for t, _ in samples) / n
    mean_s = sum(s for _, s in samples) / n
    variance = sum((t - mean_t) ** 2 for t, _ in samples)
    if variance < n:
        raise ValueError("one edge has too few visible tape pixels")
    slope = sum((t - mean_t) * (s - mean_s) for t, s in samples) / variance
    point = (a[0] + mean_t * ux - mean_s * uy, a[1] + mean_t * uy + mean_s * ux)
    return point, (ux - slope * uy, uy + slope * ux)


def intersection(first, second):
    (ax, ay), (ux, uy) = first
    (bx, by), (vx, vy) = second
    determinant = ux * vy - uy * vx
    if abs(determinant) < 0.20:
        raise ValueError("two edges do not form a corner")
    t = ((bx - ax) * vy - (by - ay) * vx) / determinant
    return ax + t * ux, ay + t * uy


def sample_tape_edges(tape, edges):
    samples = [[] for _ in edges]
    for x, y in sorted(tape)[::4]:
        distances = [abs(cross(a, b, (x, y))) / length for a, b, length in edges]
        side = min(range(len(edges)), key=distances.__getitem__)
        if distances[side] <= 5:
            offset = 3 * (y * WIDTH + x)
            samples[side].append(offset)
    if min(map(len, samples)) < 10:
        raise ValueError("too few red tape pixels are visible on one edge")
    return samples


def stable_edges(frame, samples):
    # Allow minor video jitter and evaluate each side separately, so material
    # covering an edge does not outweigh the still-visible sides of the region.
    offsets = (0, -3, 3, -3 * WIDTH, 3 * WIDTH)
    return [sum(any(is_tape(*frame[i + d:i + d + 3]) for d in offsets) for i in edge)
            >= 0.42 * len(edge) for edge in samples]


def sample_floor(frame, corners, contains, exclude_tape=False):
    floor = []
    left, right = min(x for x, _ in corners), max(x for x, _ in corners)
    top, bottom = min(y for _, y in corners), max(y for _, y in corners)
    for y in range(int(top), int(bottom) + 1, 2):
        for x in range(int(left), int(right) + 1, 2):
            if not contains(x, y):
                continue
            offset = 3 * (y * WIDTH + x)
            r, g, b = frame[offset:offset + 3]
            if max(r, g, b) < 230 and not (exclude_tape and is_tape(r, g, b)):
                floor.append((offset, x // 2, y // 2, (r + 2 * g + b) // 4, r - g, b - g))
    if len(floor) < 400:
        raise ValueError("too little clear floor inside the zone")
    return floor


def foreground_size(frame, floor, light_reference=()):
    measurements = []
    for offset, x, y, light, rg, bg in floor:
        r, g, b = frame[offset:offset + 3]
        change = (r + 2 * g + b) // 4 - light
        measurements.append((x, y, change, r - g - rg, b - g - bg))
    if light_reference:
        changes = []
        for offset, light, rg, bg in light_reference:
            r, g, b = frame[offset:offset + 3]
            changes.append(((r + 2 * g + b) // 4 - light, r - g - rg, b - g - bg))
        illumination, red_shift, blue_shift = (median(channel) for channel in zip(*changes))
    else:
        illumination = median(change for _, _, change, _, _ in measurements)
        red_shift = blue_shift = 0
    foreground = {(x, y) for x, y, change, rg, bg in measurements
                  if abs(change - illumination) >= 34
                  or abs(rg - red_shift) + abs(bg - blue_shift) >= 30}
    required = max(12, len(floor) // 320)
    return max((len(part) for part in components(foreground, required, len(floor))), default=0)


class FloorZone:
    def __init__(self):
        self.floor = []
        self.first_failed_setup = None

    def calibrate(self, frame):
        channels = zip(frame[0::3], frame[1::3], frame[2::3])
        red = {(pixel % WIDTH, pixel // WIDTH) for pixel, color in enumerate(channels)
               if is_tape(*color)}
        corners, tape = find_outline(red)
        edges = [(a, b, math.hypot(b[0] - a[0], b[1] - a[1]))
                 for a, b in zip(corners, corners[1:] + corners[:1])]
        border_samples = sample_tape_edges(tape, edges)

        def inside(x, y):
            return all(cross(a, b, (x, y)) > 1.8 * length for a, b, length in edges)

        self.floor = sample_floor(frame, corners, inside, exclude_tape=True)
        self.anchors = border_samples
        return corners

    def __call__(self, frame):
        if not self.floor:
            try:
                corners = self.calibrate(frame)
            except ValueError as error:
                now = time.monotonic()
                if self.first_failed_setup is None:
                    self.first_failed_setup = now
                if now - self.first_failed_setup < 3:
                    return UNKNOWN, "looking for the four red edges"
                raise RuntimeError("Zone setup failed: {}. Show all four red corners and restart with the zone empty.".format(error)) from error
            detail = " ".join("({:.0f},{:.0f})".format(x, y) for x, y in corners)
            print("Zone calibrated (480x270): " + detail, flush=True)

        visible = stable_edges(frame, self.anchors)
        if not any(visible[i - 1] and visible[i] for i in range(len(visible))):
            return UNKNOWN, "{} of 4 red edges visible; check for blockage or camera movement".format(sum(visible))

        largest = foreground_size(frame, self.floor)
        if largest:
            return "INTRUSION", "foreground inside red zone, {} samples".format(largest)
        return "CLEAR", "red zone clear"


if __name__ == "__main__":
    sys.exit(main(FloorZone(), zone=True))
