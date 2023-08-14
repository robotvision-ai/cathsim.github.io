import numpy as np
import trimesh
import random


from dm_control import mjcf
from dm_control.mujoco import wrapper
from dm_control import composer
from dm_control.composer import variation
from dm_control.composer.variation import distributions, noises
from dm_control.composer.observation import observable

from cathsim.phantom import Phantom
from cathsim.utils import distance
from cathsim.guidewire import Guidewire, Tip
from cathsim.observables import CameraObservable

from cathsim.utils import filter_mask, point2pixel, get_env_config

env_config = get_env_config()

option = env_config["option"]
option_flag = option.pop("flag")
compiler = env_config["compiler"]
visual = env_config["visual"]
visual_global = visual.pop("global")
guidewire_config = env_config["guidewire"]

BODY_DIAMETER = guidewire_config["diameter"] * guidewire_config["scale"]
SPHERE_RADIUS = (BODY_DIAMETER / 2) * guidewire_config["scale"]
CYLINDER_HEIGHT = SPHERE_RADIUS * guidewire_config["sphere_to_cylinder_ratio"]
OFFSET = SPHERE_RADIUS + CYLINDER_HEIGHT * 2


random_state = np.random.RandomState(42)


def make_scene(geom_groups: list):
    scene_option = wrapper.MjvOption()
    scene_option.geomgroup = np.zeros_like(scene_option.geomgroup)
    for geom_group in geom_groups:
        scene_option.geomgroup[geom_group] = True
    return scene_option


def sample_points(
    mesh: trimesh.Trimesh, y_bounds: tuple, n_points: int = 10
) -> np.array:
    """
    Samples points within the mesh volume

    :param mesh: trimesh.Trimesh
    :param y_bounds: tuple:
    :param n_points: int:  (Default value = 10)

    """

    def is_within_limits(point: list) -> bool:
        """
        Check if a point is within limits.

        :param point: list:

        """
        return y_bounds[0] < point[1] < y_bounds[1]

    while True:
        points = trimesh.sample.volume_mesh(mesh, n_points)
        if len(points) == 0:
            continue
        valid_points = [point for point in points if is_within_limits(point)]
        if len(valid_points) == 0:
            continue
        elif len(valid_points) == 1:
            return valid_points[0]
        else:
            return random.choice(valid_points)


class Scene(composer.Arena):
    """
    The main Scene of the environment. It sets the main properties such as the compiler settings.
    """

    def _build(
        self,
        name: str = "arena",
        render_site: bool = False,
    ):
        """

        :param name: str:  (Default value = "arena")
        :param render_site: bool:  (Default value = False)

        """
        super()._build(name=name)

        self._mjcf_root.compiler.set_attributes(**compiler)
        self._mjcf_root.option.set_attributes(**option)
        self._mjcf_root.option.flag.set_attributes(**option_flag)
        self._mjcf_root.visual.set_attributes(**visual)

        self._top_camera = self.add_camera(
            "top_camera", [-0.03, 0.125, 0.15], [0, 0, 0]
        )
        self._top_camera_close = self.add_camera(
            "top_camera_close", [-0.03, 0.125, 0.065], [0, 0, 0]
        )
        self._mjcf_root.default.site.set_attributes(
            type="sphere",
            size=[0.002],
            rgba=[0.8, 0.8, 0.8, 0],
        )

        self._mjcf_root.asset.add(
            "texture",
            type="skybox",
            builtin="gradient",
            rgb1=[1, 1, 1],
            rgb2=[1, 1, 1],
            width=256,
            height=256,
        )

        self.add_light(pos=[0, 0, 10], dir=[20, 20, -20], castshadow=False)

    def add_light(
        self, pos: list = [0, 0, 0], dir: list = [0, 0, 0], castshadow: bool = False
    ) -> mjcf.Element:
        """
        Adds a light element

        :param pos: list:  (Default value = [0, 0, 0])
        :param dir: list:  (Default value = [0, 0, 0])
        :param castshadow: bool:  (Default value = False)

        """
        light = self._mjcf_root.worldbody.add(
            "light", pos=pos, dir=dir, castshadow=castshadow
        )
        return light

    def add_camera(
        self, name: str, pos: list = [0, 0, 0], euler: list = [0, 0, 0]
    ) -> mjcf.Element:
        """
        Adds a camera element

        :param name: str:
        :param pos: list:  (Default value = [0, 0, 0])
        :param euler: list:  (Default value = [0, 0, 0])

        """
        camera = self._mjcf_root.worldbody.add(
            "camera", name=name, pos=pos, euler=euler
        )
        return camera

    def add_site(self, name: str, pos: list = [0, 0, 0]) -> mjcf.Element:
        """
        Adds a site.

        :param name: str:
        :param pos: list:  (Default value = [0, 0, 0])

        """
        site = self._mjcf_root.worldbody.add("site", name=name, pos=pos)
        return site


class UniformCircle(variation.Variation):
    def __init__(
        self,
        x_range: tuple[int] = (-0.001, 0.001),
        y_range: tuple[int] = (-0.001, 0.001),
        z_range: tuple[int] = (-0.001, 0.001),
    ):
        self._x_distrib = distributions.Uniform(*x_range)
        self._y_distrib = distributions.Uniform(*y_range)
        self._z_distrib = distributions.Uniform(*z_range)

    def __call__(self, initial_value=None, current_value=None, random_state=None):
        x_pos = variation.evaluate(self._x_distrib, random_state=random_state)
        y_pos = variation.evaluate(self._y_distrib, random_state=random_state)
        z_pos = variation.evaluate(self._z_distrib, random_state=random_state)
        return (x_pos, y_pos, z_pos)


class Navigate(composer.Task):
    """
    The task class. It is responsible for adding all the elements (phantom, guidewire) together.
    """

    def __init__(
        self,
        phantom: composer.Entity = None,
        guidewire: composer.Entity = None,
        tip: composer.Entity = None,
        delta: float = 0.004,  # distance threshold for success
        dense_reward: bool = True,
        success_reward: float = 10.0,
        use_pixels: bool = False,
        use_segment: bool = False,
        use_phantom_segment: bool = False,
        image_size: int = 80,
        sample_target: bool = False,
        visualize_sites: bool = False,
        target_from_sites: bool = True,
        random_init_distance: float = 0.001,
        target=None,
    ):
        self.delta = delta
        self.dense_reward = dense_reward
        self.success_reward = success_reward
        self.use_pixels = use_pixels
        self.use_segment = use_segment
        self.use_phantom_segment = use_phantom_segment
        self.image_size = image_size
        self.sample_target = sample_target
        self.visualize_sites = visualize_sites
        self.target_from_sites = target_from_sites
        self.random_init_distance = random_init_distance

        self._arena = Scene("arena")
        if phantom is not None:
            self._phantom = phantom
            self._arena.attach(self._phantom)
        if guidewire is not None:
            self._guidewire = guidewire
            if tip is not None:
                self._tip = tip
                self._guidewire.attach(self._tip)
            self._arena.attach(self._guidewire)

        # Configure initial poses
        self._guidewire_initial_pose = UniformCircle(
            x_range=(-random_init_distance, random_init_distance),
            y_range=(-random_init_distance, random_init_distance),
            z_range=(-random_init_distance, random_init_distance),
        )

        # Configure variators
        self._mjcf_variator = variation.MJCFVariator()
        self._physics_variator = variation.PhysicsVariator()

        pos_corrptor = noises.Additive(distributions.Normal(scale=0.0001))
        vel_corruptor = noises.Multiplicative(distributions.LogNormal(sigma=0.0001))

        self._task_observables = {}

        if self.use_pixels:
            self._task_observables["pixels"] = CameraObservable(
                camera_name="top_camera",
                width=image_size,
                height=image_size,
            )

        if self.use_segment:
            guidewire_option = make_scene([1, 2])

            self._task_observables["guidewire"] = CameraObservable(
                camera_name="top_camera",
                height=image_size,
                width=image_size,
                scene_option=guidewire_option,
                segmentation=True,
            )

        if self.use_phantom_segment:
            phantom_option = make_scene([0])
            self._task_observables["phantom"] = CameraObservable(
                camera_name="top_camera",
                height=image_size,
                width=image_size,
                scene_option=phantom_option,
                segmentation=True,
            )

        self._task_observables["joint_pos"] = observable.Generic(
            self.get_joint_positions
        )
        self._task_observables["joint_vel"] = observable.Generic(
            self.get_joint_velocities
        )

        self._task_observables["joint_pos"].corruptor = pos_corrptor
        self._task_observables["joint_vel"].corruptor = vel_corruptor

        for obs in self._task_observables.values():
            obs.enabled = True

        self.control_timestep = env_config["num_substeps"] * self.physics_timestep

        self.success = False

        self.set_target(target)
        self.camera_matrix = None

        if self.visualize_sites:
            sites = self._phantom._mjcf_root.find_all("site")
            for site in sites:
                site.rgba = [1, 0, 0, 1]

    @property
    def root_entity(self):
        return self._arena

    @property
    def task_observables(self):
        return self._task_observables

    @property
    def target_pos(self):
        """The target_pos property."""
        return self._target_pos

    def set_target(self, target) -> None:
        """target is one of:
        - str: name of the site
        - np.ndarray: target position

        :param target:

        """

        if type(target) is str:
            sites = self._phantom.sites
            assert (
                target in sites
            ), f"Target site not found. Valid sites are: {sites.keys()}"
            target = sites[target]
        self._target_pos = target

    def initialize_episode_mjcf(self, random_state):
        self._mjcf_variator.apply_variations(random_state)

    def initialize_episode(self, physics, random_state):
        if self.camera_matrix is None:
            self.camera_matrix = self.get_camera_matrix(physics)
        self._physics_variator.apply_variations(physics, random_state)
        guidewire_pose = variation.evaluate(
            self._guidewire_initial_pose, random_state=random_state
        )
        self._guidewire.set_pose(physics, position=guidewire_pose)
        self.success = False
        if self.sample_target:
            self.set_target(self.get_random_target(physics))

    def get_reward(self, physics):
        self.head_pos = self.get_head_pos(physics)
        reward = self.compute_reward(self.head_pos, self._target_pos)
        return reward

    def should_terminate_episode(self, physics):
        return self.success

    def get_head_pos(self, physics):
        return physics.named.data.geom_xpos[-1]

    def compute_reward(self, achieved_goal, desired_goal):
        d = distance(achieved_goal, desired_goal)
        success = np.array(d < self.delta, dtype=bool)

        if self.dense_reward:
            reward = np.where(success, self.success_reward, -d)
        else:
            reward = np.where(success, self.success_reward, -1.0)
        self.success = success
        return reward

    def get_joint_positions(self, physics):
        positions = physics.named.data.qpos
        return positions

    def get_joint_velocities(self, physics):
        velocities = physics.named.data.qvel
        return velocities

    def get_force(self, physics):
        forces = physics.data.qfrc_constraint[0:3]
        forces = np.linalg.norm(forces)
        return forces

    def get_contact_forces(
        self, physics, threshold=0.01, to_pixels=True, image_size=64
    ):
        """
        Extracts the contact forces.

        :param physics:
        :param threshold:  (Default value = 0.01)
        :param to_pixels:  (Default value = True)
        :param image_size:  (Default value = 64)

        """
        if self.camera_matrix is None:
            self.camera_matrix = self.get_camera_matrix(physics, image_size)
        data = physics.data
        forces = {"pos": [], "force": []}
        for i in range(data.ncon):
            if data.contact[i].dist < 0.002:
                force = data.contact_force(i)[0][0]
                if abs(force) > threshold:
                    pass
                else:
                    forces["force"].append(force)
                    pos = data.contact[i].pos
                    if to_pixels is not None:
                        pos = point2pixel(pos, self.camera_matrix)
                    forces["pos"].append(pos)
        return forces

    def get_camera_matrix(self, physics, image_size: int = None, camera_id=0):
        """
        Extracts the camera matrix.

        :param physics:
        :param image_size: int:  (Default value = None)
        :param camera_id:  (Default value = 0)

        """
        from dm_control.mujoco.engine import Camera

        if image_size is None:
            image_size = self.image_size
        camera = Camera(
            physics, height=image_size, width=image_size, camera_id=camera_id
        )
        return camera.matrix

    def get_phantom_mask(self, physics, image_size: int = None, camera_id=0):
        """
        Extracts the phantom segmentation mask.

        :param physics:
        :param image_size: int:  (Default value = None)
        :param camera_id:  (Default value = 0)

        """
        scene_option = make_scene([0])
        if image_size is None:
            image_size = self.image_size
        image = physics.render(
            height=image_size,
            width=image_size,
            camera_id=camera_id,
            scene_option=scene_option,
        )
        mask = filter_mask(image)
        return mask

    def get_guidewire_mask(self, physics, image_size: int = None, camera_id=0):
        """
        Extracts the guidewire mask.

        :param physics:
        :param image_size: int:  (Default value = None)
        :param camera_id:  (Default value = 0)

        """
        scene_option = make_scene([1, 2])
        if image_size is None:
            image_size = self.image_size
        image = physics.render(
            height=image_size,
            width=image_size,
            camera_id=camera_id,
            scene_option=scene_option,
        )
        mask = filter_mask(image)
        return mask

    def get_random_target(self, physics):
        """
        Samples a target.

        :param physics:

        """
        if self.target_from_sites:
            sites = self._phantom.sites
            site = np.random.choice(list(sites.keys()))
            target = sites[site]
            return target
        mesh = trimesh.load_mesh(self._phantom.simplified, scale=0.9)
        return sample_points(mesh, (0.0954, 0.1342))

    def get_guidewire_geom_pos(self, physics):
        model = physics.copy().model
        guidewire_geom_ids = [
            model.geom(i).id
            for i in range(model.ngeom)
            if "guidewire" in model.geom(i).name
        ]
        guidewire_geom_pos = [physics.data.geom_xpos[i] for i in guidewire_geom_ids]
        return guidewire_geom_pos


def run_env(args=None):
    """
    Runs the environment.

    :param args:  (Default value = None)

    """
    from argparse import ArgumentParser
    from dm_control.viewer import launch

    parser = ArgumentParser()
    parser.add_argument("--interact", type=bool, default=True)
    parser.add_argument("--phantom", default="phantom3", type=str)
    parser.add_argument("--target", default="bca", type=str)

    parsed_args = parser.parse_args(args)

    phantom = Phantom(parsed_args.phantom + ".xml")

    tip = Tip()
    guidewire = Guidewire()

    task = Navigate(
        phantom=phantom,
        guidewire=guidewire,
        tip=tip,
        use_pixels=True,
        use_segment=True,
        target=parsed_args.target,
        visualize_sites=True,
    )

    env = composer.Environment(
        task=task,
        time_limit=2000,
        random_state=np.random.RandomState(42),
        strip_singleton_obs_buffer_dim=True,
    )

    def random_policy(time_step):
        """

        :param time_step:

        """
        del time_step  # Unused
        return [0, 0]

    if parsed_args.interact:
        from cathsim.utils import launch

        launch(env)
    else:
        launch(env, policy=random_policy)


if __name__ == "__main__":
    phantom_name = "phantom3"
    phantom = Phantom(phantom_name + ".xml")
    tip = Tip()
    guidewire = Guidewire()

    task = Navigate(
        phantom=phantom,
        guidewire=guidewire,
        tip=tip,
        use_pixels=True,
        use_segment=True,
        target="bca",
        image_size=80,
        visualize_sites=False,
        sample_target=True,
        target_from_sites=False,
    )

    env = composer.Environment(
        task=task,
        time_limit=2000,
        random_state=np.random.RandomState(42),
        strip_singleton_obs_buffer_dim=True,
    )

    env._task.get_guidewire_geom_pos(env.physics)
    exit()

    def random_policy(time_step):
        """

        :param time_step:

        """
        del time_step  # Unused
        return [0, 0]

    # loop 2 episodes of 2 steps
    for episode in range(2):
        time_step = env.reset()
        # print(env._task.target_pos)
        # print(env._task.get_head_pos(env._physics))
        print(env._task.get_camera_matrix(env.physics, 480))
        print(env._task.get_camera_matrix(env.physics, 80))
        exit()
        for step in range(2):
            action = random_policy(time_step)
            img = env.physics.render(height=480, width=480, camera_id=0)
            # plt.imsave("phantom_480.png", img)
            time_step = env.step(action)