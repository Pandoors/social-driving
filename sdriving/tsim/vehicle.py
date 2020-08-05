import math
from typing import List, Optional, Union

import matplotlib.pyplot as plt
import numpy as np
import torch
from sdriving.tsim.utils import (
    angle_normalize,
    circle_area_overlap,
    transform_2d_coordinates,
    check_intersection_lines,
)


class _BatchedVehicle(torch.nn.Module):
    """
        A fleet of vehicles. A single vehicle is a batched vehicle with
        1 as the batch size
    """

    def __init__(
        self,
        position: torch.Tensor,  # N x 2
        orientation: torch.Tensor,  # N x 1
        destination: torch.Tensor,  # N x 2
        dest_orientation: torch.Tensor,  # N x 1
        dimensions: torch.Tensor = torch.as_tensor([[4.48, 2.2]]),  # N x 2
        initial_speed: torch.Tensor = torch.zeros(1, 1),  # N x 1
        name: List[str] = ["car"],  # N
        min_lidar_range: float = 2.5,
        max_lidar_range: float = 50.0,
        vision_range: float = 50.0,
    ):
        super().__init__()
        self.name = name

        self.position = position
        self.orientation = angle_normalize(orientation)
        self.destination = destination
        self.dest_orientation = angle_normalize(dest_orientation)
        self.dimensions = dimensions

        self.nbatch = self.position.size(0)
        self.diag_bool_buffer = ~torch.diag(torch.ones(self.nbatch * 4)).bool()

        self.speed = initial_speed
        self.safety_circle = (
            1.3
            * torch.sqrt(
                ((self.dimensions / 2) ** 2).sum(1, keepdim=True)
            ).detach()
        )
        self.area = math.pi * self.safety_circle ** 2

        mul_factor = (
            torch.as_tensor([[1, 1], [1, -1], [-1, -1], [-1, 1]])
            .unsqueeze(0)
            .type_as(self.dimensions)
        )

        self.base_coordinates = mul_factor * self.dimensions.unsqueeze(1) / 2
        self.device = torch.device("cpu")

        self.to(self.position.device)

        self.cached_coordinates = False
        self.coordinates = self._get_coordinates()

        self.max_lidar_range = max_lidar_range
        self.min_lidar_range = min_lidar_range
        self.vision_range = vision_range

    def to(self, device: torch.device):
        if device == self.device:
            return
        for k, t in self.__dict__.items():
            if torch.is_tensor(t):
                setattr(self, k, t.to(device))
        self.device = device

    @torch.jit.export
    def _get_coordinates(self):
        self.cached_coordinates = True
        return transform_2d_coordinates(
            self.base_coordinates,
            self.orientation[:, 0],
            self.position[:, None, :],
        )

    @torch.jit.export
    def get_coordinates(self):
        return (
            self.coordinates
            if self.cached_coordinates
            else self._get_coordinates()
        )

    @torch.jit.export
    def get_edges(self):
        coordinates = self.get_coordinates()
        pt1 = coordinates
        pt2 = torch.cat([coordinates[:, :, 1:], coordinates[:, :, 0:1]], dim=1)
        return pt1, pt2  # N x 4 x 2, N x 4 x 2

    @torch.jit.export
    def update_state(self, state: torch.Tensor):
        """
        Args:
            state: {x coordinate, y coordinate, speed, orientation}
        """
        self.position = state[:, :2]
        self.speed = state[:, 2:3]
        self.orientation = state[:, 3:4]
        self.cached_coordinates = False

    @torch.jit.export
    def get_state(self):
        return torch.cat([self.position, self.speed, self.orientation], dim=-1)

    @torch.jit.export
    def distance_from_point(self, point: torch.Tensor):
        return (self.position - point).pow(2).sum(1, keepdim=True).sqrt()

    @torch.jit.export
    def distance_from_destination(self):
        return self.distance_from_point(self.destination)

    @torch.jit.export
    def optimal_heading_to_point(self, point: torch.Tensor):
        vec = point - self.position
        vec = vec / (torch.norm(vec, dim=1) + 1e-7)  # N x 2
        cur_vec = torch.cat(
            [torch.cos(self.orientation), torch.sin(self.orientation)]
        )  # N x 2
        return angle_normalize(
            torch.acos(
                (vec * cur_vec)
                .sum(1, keepdim=True)
                .clamp(-1.0 + 1e-5, 1.0 - 1e-5)
            )
        )

    @torch.jit.export
    def optimal_heading(self):
        return self.optimal_heading_to_point(self.destination)

    @torch.jit.export
    def collision_check(self):
        p1, p2 = self.get_edges()
        p1, p2 = p1.view(-1, 2), p2.view(-1, 2)

        c = check_intersection_lines(p1, p2, p1, p2) * self.diag_bool_buffer
        return c.view(self.nbatch, 4, -1).any(1).any(1)


def BatchedVehicle(*args, **kwargs):
    return torch.jit.script(_BatchedVehicle(*args, **kwargs))


class _Vehicle(_BatchedVehicle):
    def __init__(
        self,
        position: torch.Tensor,  # 2
        orientation: torch.Tensor,  # 1
        destination: torch.Tensor,  # 2
        dest_orientation: torch.Tensor,  # 1
        dimensions: torch.Tensor = torch.as_tensor([4.48, 2.2]),  # 2
        initial_speed: torch.Tensor = torch.zeros(1),  # 1
        name: str = "car",
        min_lidar_range: float = 1.0,
        max_lidar_range: float = 50.0,
        vision_range: float = 50.0,
    ):
        super().__init__(
            position.unsqueeze(0),
            orientation.unsqueeze(0),
            destination.unsqueeze(0),
            dest_orientation.unsqueeze(0),
            dimensions.unsqueeze(0),
            initial_speed.unsqueeze(0),
            [name],
            min_lidar_range,
            max_lidar_range,
            vision_range,
        )


def Vehicle(*args, **kwargs):
    return torch.jit.script(_Vehicle(*args, **kwargs))


def render_vehicle(
    obj: Union[_BatchedVehicle, _Vehicle],
    ax,
    color: str = "g",
    draw_lidar_range: bool = True,
):
    for b in range(obj.nbatch):
        pos = obj.position[b, :].detach().cpu().numpy()
        h = obj.orientation[b, :].detach().cpu().numpy()
        dim = obj.dimensions[b, 0].item()
        box = obj.get_coordinates()[b, :, :].detach().cpu().numpy()
        lr = obj.max_lidar_range

        arrow = np.array(
            [pos, pos + dim / 2.0 * np.array([np.cos(h), np.sin(h)])]
        )

        # Draw the vehicle and the heading
        plt.fill(box[:, 0], box[:, 1], color, edgecolor="k", alpha=0.5)
        plt.plot(arrow[:, 0], arrow[:, 1], "b")

        # Draw the destination if available
        dest = obj.destination[b, :].detach().cpu().numpy()
        plt.plot(dest[0], dest[1], color, marker="x", markersize=5)

        # Draw the lidar sensor range
        if draw_lidar_range:
            ax.add_artist(
                plt.Circle(pos, lr, color="b", fill=False, linestyle="--")
            )


@torch.jit.export
def safety_circle_overlap(obj1: _BatchedVehicle, obj2: _BatchedVehicle):
    center1 = obj1.position.repeat(obj2.nbatch, 1)
    center2 = obj2.position.unsqueeze(0).repeat(obj1.nbatch, 1, 1).view(-1, 2)
    radius1 = obj1.safety_circle.repeat(obj2.nbatch, 1)
    radius2 = (
        obj2.safety_circle.unsqueeze(0).repeat(obj1.nbatch, 1, 1).view(-1, 1)
    )
    return circle_area_overlap(center1, center2, radius1, radius2).view(
        obj1.nbatch, obj2.nbatch
    )
