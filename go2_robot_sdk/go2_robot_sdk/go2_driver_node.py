# Copyright (c) 2024, RoboVerse community
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


import json
import logging
import os
import threading
import asyncio

from scripts.go2_constants import ROBOT_CMD, RTC_TOPIC
from scripts.go2_func import gen_command, gen_mov_command
from scripts.go2_lidar_decoder import update_meshes_for_cloud2
from scripts.go2_math import get_robot_joints
from scripts.webrtc_driver import Go2Connection

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile

from tf2_ros import TransformBroadcaster, TransformStamped
from geometry_msgs.msg import Twist, TransformStamped
from go2_interfaces.msg import Go2State, IMU
from sensor_msgs.msg import PointCloud2, PointField, JointState, Joy
from sensor_msgs_py import point_cloud2
from std_msgs.msg import Header, String
from nav_msgs.msg import Odometry


logging.basicConfig(level=logging.WARN)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class RobotBaseNode(Node):

    def __init__(self):
        super().__init__('go2_driver_node')

        self.declare_parameter('robot_ip', os.getenv('ROBOT_IP', os.getenv('GO2_IP')))
        self.declare_parameter('token', os.getenv('ROBOT_TOKEN', os.getenv('GO2_TOKEN','')))
        
        self.robot_ip = self.get_parameter('robot_ip').get_parameter_value().string_value
        self.token = self.get_parameter('token').get_parameter_value().string_value
        self.robot_ip_lst = self.robot_ip.replace(" ", "").split(",")

        self.get_logger().info(f"Received ip list: {self.robot_ip_lst}")

        self.conn = []
        qos_profile = QoSProfile(depth=10)

        self.joint_pub = []
        self.go2_state_pub = []
        self.go2_lidar_pub = []
        self.go2_odometry_pub = []
        self.imu_pub = []

        for i in range(len(self.robot_ip_lst)):
            self.joint_pub.append(self.create_publisher(JointState, f'robot{i}/joint_states', qos_profile))
            self.go2_state_pub.append(self.create_publisher(Go2State, f'robot{i}/go2_states', qos_profile))
            self.go2_lidar_pub.append(self.create_publisher(PointCloud2, f'robot{i}/point_cloud2', qos_profile))
            self.go2_odometry_pub.append(self.create_publisher(Odometry, f'robot{i}/odom', qos_profile))
            self.imu_pub.append(self.create_publisher(IMU, f'robot{i}/imu', qos_profile))
        
        
        self.broadcaster = TransformBroadcaster(self, qos=qos_profile)

        self.robot_cmd_vel = {}
        self.robot_odom = {}
        self.robot_low_cmd = {}
        self.robot_sport_state = {}
        self.robot_lidar = {}
        
        self.joy_state = Joy()


        for i in range(len(self.robot_ip_lst)):
            self.create_subscription(
                Twist,
                f'robot{i}/cmd_vel',
                lambda msg: self.cmd_vel_cb(msg, i),
                qos_profile)
                    
        self.create_subscription(
            Joy,
            'joy',
            self.joy_cb,
            qos_profile)
        
        self.timer = self.create_timer(0.1, self.timer_callback)
        self.timer_lidar = self.create_timer(0.5, self.timer_callback_lidar)

    def timer_callback(self):
        self.publish_odom()
        self.publish_odom_topic()
        self.publish_robot_state()
        self.publish_joint_state()

    def timer_callback_lidar(self):
        self.publish_lidar()

    def cmd_vel_cb(self, msg, robot_num):
        robot_num = str(robot_num)
        x = msg.linear.x
        y = msg.linear.y
        z = msg.angular.z
        if x > 0.0 or y > 0.0 or z > 0.0:
            if robot_num in self.robot_cmd_vel:
                self.robot_cmd_vel[robot_num] = gen_mov_command(x, y, z)


    def joy_cb(self, msg):
        self.joy_state = msg

    def joy_cmd(self, robot_num):
        if str(robot_num) in self.robot_cmd_vel and robot_num in self.conn and self.robot_cmd_vel[str(robot_num)]:
            self.get_logger().info("Attack!")
            self.conn[robot_num].data_channel.send(self.robot_cmd_vel[str(robot_num)])
            self.robot_cmd_vel[str(robot_num)] = None

        if robot_num in self.conn and self.joy_state.buttons and self.joy_state.buttons[1]:
            self.get_logger().info("Stand down")
            stand_down_cmd = gen_command(ROBOT_CMD["StandDown"])
            self.conn[robot_num].data_channel.send(stand_down_cmd)

        if robot_num in self.conn and self.joy_state.buttons and self.joy_state.buttons[0]:
            self.get_logger().info("Stand up")
            stand_up_cmd = gen_command(ROBOT_CMD["StandUp"])
            self.conn[robot_num].data_channel.send(stand_up_cmd)
            balance_stand_cmd = gen_command(ROBOT_CMD['BalanceStand'])
            self.conn[robot_num].data_channel.send(balance_stand_cmd)

    def on_validated(self, robot_num):
        self.get_logger().info("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        for topic in RTC_TOPIC.values():
            self.conn[robot_num].data_channel.send(json.dumps({"type": "subscribe", "topic": topic}))
        
    def on_data_channel_message(self, _, msg, robot_num):
        self.get_logger().info(str(msg))
        robot_num = str(robot_num)
        
        if robot_num in self.robot_lidar and msg.get('topic') == RTC_TOPIC["ULIDAR_ARRAY"]:
            self.robot_lidar[robot_num] = msg

        if robot_num in self.robot_odom and msg.get('topic') == RTC_TOPIC['ROBOTODOM']:
            self.robot_odom[robot_num] = msg

        if robot_num in self.robot_sport_state and msg.get('topic') == RTC_TOPIC['LF_SPORT_MOD_STATE']:
            self.robot_sport_state[robot_num] = msg
            
        if robot_num in self.robot_low_cmd and msg.get('topic') == RTC_TOPIC['LOW_STATE']:
            self.robot_low_cmd[robot_num] = msg

    def publish_odom(self):
        self.get_logger().info(str(self.robot_odom))
        self.get_logger().info(str(len(self.robot_odom)))
        for i in range(len(self.robot_odom)):
            if self.robot_odom[i]:
                odom_trans = TransformStamped()
                odom_trans.header.stamp = self.get_clock().now().to_msg()
                odom_trans.header.frame_id = 'odom'
                odom_trans.child_frame_id = f"robot{i}/base_link"
                odom_trans.transform.translation.x = self.robot_odom[i]['data']['pose']['position']['x']
                odom_trans.transform.translation.y = self.robot_odom[i]['data']['pose']['position']['y']
                odom_trans.transform.translation.z = self.robot_odom[i]['data']['pose']['position']['z'] + 0.07
                odom_trans.transform.rotation.x = self.robot_odom[i]['data']['pose']['orientation']['x']
                odom_trans.transform.rotation.y = self.robot_odom[i]['data']['pose']['orientation']['y']
                odom_trans.transform.rotation.z = self.robot_odom[i]['data']['pose']['orientation']['z']
                odom_trans.transform.rotation.w = self.robot_odom[i]['data']['pose']['orientation']['w']
                self.broadcaster.sendTransform(odom_trans)
    
    def publish_odom_topic(self):
        for i in range(len(self.robot_odom)):
            if self.robot_odom[i]:
                odom_msg = Odometry()
                odom_msg.header.stamp = self.get_clock().now().to_msg()
                odom_msg.header.frame_id = 'odom'
                odom_msg.child_frame_id = f"robot{i}/base_link"
                odom_msg.pose.pose.position.x = self.robot_odom[i]['data']['pose']['position']['x']
                odom_msg.pose.pose.position.y = self.robot_odom[i]['data']['pose']['position']['y']
                odom_msg.pose.pose.position.z = self.robot_odom[i]['data']['pose']['position']['z'] + 0.07
                odom_msg.pose.pose.orientation.x = self.robot_odom[i]['data']['pose']['orientation']['x']
                odom_msg.pose.pose.orientation.y = self.robot_odom[i]['data']['pose']['orientation']['y']
                odom_msg.pose.pose.orientation.z = self.robot_odom[i]['data']['pose']['orientation']['z']
                odom_msg.pose.pose.orientation.w = self.robot_odom[i]['data']['pose']['orientation']['w']
                self.go2_odometry_pub[i].publish(odom_msg)

    def publish_lidar(self):
        for i in range(len(self.robot_lidar)):
            if self.robot_lidar[i]:
                points = update_meshes_for_cloud2(
                    self.robot_lidar[i]["decoded_data"]["positions"], 
                    self.robot_lidar[i]["decoded_data"]["uvs"], 
                    self.robot_lidar[i]['data']['resolution'], 
                    self.robot_lidar[i]['data']['origin'],
                    0
                    )
                point_cloud = PointCloud2()
                point_cloud.header = Header(frame_id="odom")
                point_cloud.header.stamp = self.get_clock().now().to_msg()
                fields = [
                    PointField(name='x', offset=0, datatype=PointField.FLOAT32, count=1),
                    PointField(name='y', offset=4, datatype=PointField.FLOAT32, count=1),
                    PointField(name='z', offset=8, datatype=PointField.FLOAT32, count=1),
                    PointField(name='intensity', offset=12, datatype=PointField.FLOAT32, count=1),
                ]
                point_cloud = point_cloud2.create_cloud(point_cloud.header, fields, points)
                self.go2_lidar_pub[i].publish(point_cloud)

    def publish_joint_state(self): 
        for i in range(len(self.robot_sport_state)):
            if self.robot_sport_state[i]:
                joint_state = JointState()
                joint_state.header.stamp = self.get_clock().now().to_msg()

                fl_foot_pos_array = [
                    self.robot_sport_state[i]["data"]["foot_position_body"][3], 
                    self.robot_sport_state[i]["data"]["foot_position_body"][4], 
                    self.robot_sport_state[i]["data"]["foot_position_body"][5]
                ]

                FL_hip_joint, FL_thigh_joint, FL_calf_joint = get_robot_joints(
                    fl_foot_pos_array,
                    0
                    )
                
                fr_foot_pos_array = [
                    self.robot_sport_state[i]["data"]["foot_position_body"][0], 
                    self.robot_sport_state[i]["data"]["foot_position_body"][1], 
                    self.robot_sport_state[i]["data"]["foot_position_body"][2]
                ]

                FR_hip_joint, FR_thigh_joint, FR_calf_joint = get_robot_joints(
                    fr_foot_pos_array,
                    1
                    )
                
                rl_foot_pos_array = [
                    self.robot_sport_state[i]["data"]["foot_position_body"][9], 
                    self.robot_sport_state[i]["data"]["foot_position_body"][10], 
                    self.robot_sport_state[i]["data"]["foot_position_body"][11]
                ]

                RL_hip_joint, RL_thigh_joint, RL_calf_joint = get_robot_joints(
                    rl_foot_pos_array,
                    2
                    )
                
                rr_foot_pos_array = [
                    self.robot_sport_state[i]["data"]["foot_position_body"][6], 
                    self.robot_sport_state[i]["data"]["foot_position_body"][7], 
                    self.robot_sport_state[i]["data"]["foot_position_body"][8]
                ]

                RR_hip_joint, RR_thigh_joint, RR_calf_joint = get_robot_joints(
                    rr_foot_pos_array,
                    3
                    )
                
                joint_state.name = [
                    f'robot{i}/FL_hip_joint', f'robot{i}/FL_thigh_joint', f'robot{i}/FL_calf_joint',
                    f'robot{i}/FR_hip_joint', f'robot{i}/FR_thigh_joint', f'robot{i}/FR_calf_joint',
                    f'robot{i}/RL_hip_joint', f'robot{i}/RL_thigh_joint', f'robot{i}/RL_calf_joint',
                    f'robot{i}/RR_hip_joint', f'robot{i}/RR_thigh_joint', f'robot{i}/RR_calf_joint',
                    ]
                joint_state.position = [
                    FL_hip_joint, FL_thigh_joint, FL_calf_joint,
                    FR_hip_joint, FR_thigh_joint, FR_calf_joint,
                    RL_hip_joint, RL_thigh_joint, RL_calf_joint,
                    RR_hip_joint, RR_thigh_joint, RR_calf_joint,
                    ]
                self.joint_pub[i].publish(joint_state) 

    def publish_robot_state(self):
        for i in range(len(self.robot_sport_state)):
            if self.robot_sport_state[i]:
                go2_state = Go2State()
                go2_state.mode = self.robot_sport_state[i]["data"]["mode"]
                go2_state.progress = self.robot_sport_state[i]["data"]["progress"]
                go2_state.gait_type = self.robot_sport_state[i]["data"]["gait_type"]
                go2_state.position = list(map(float,self.robot_sport_state[i]["data"]["position"]))
                go2_state.body_height = float(self.robot_sport_state[i]["data"]["body_height"])
                go2_state.velocity = self.robot_sport_state[i]["data"]["velocity"]
                go2_state.range_obstacle = list(map(float, self.robot_sport_state[i]["data"]["range_obstacle"]))
                go2_state.foot_force = self.robot_sport_state[i]["data"]["foot_force"]
                go2_state.foot_position_body = list(map(float,self.robot_sport_state[i]["data"]["foot_position_body"]))
                go2_state.foot_speed_body = list(map(float, self.robot_sport_state[i]["data"]["foot_speed_body"]))
                self.go2_state_pub[i].publish(go2_state) 

                imu = IMU()
                imu.quaternion = list(map(float,self.robot_sport_state[i]["data"]["imu_state"]["quaternion"]))
                imu.accelerometer = list(map(float,self.robot_sport_state[i]["data"]["imu_state"]["accelerometer"]))
                imu.gyroscope = list(map(float,self.robot_sport_state[i]["data"]["imu_state"]["gyroscope"]))
                imu.rpy = list(map(float,self.robot_sport_state[i]["data"]["imu_state"]["rpy"]))
                imu.temperature = self.robot_sport_state[i]["data"]["imu_state"]["temperature"]
                self.imu_pub[i].publish(imu) 

    async def run(self, conn, robot_num):
        self.conn.append(conn)
        await self.conn[robot_num].connect()
        self.get_logger().info(f"Connected to {os.environ.get('ROBOT_IP')}")

        while True:
            self.joy_cmd(robot_num)
            await asyncio.sleep(0.1)


async def spin(node: Node):
    cancel = node.create_guard_condition(lambda: None)
    def _spin(node: Node,
              future: asyncio.Future,
              event_loop: asyncio.AbstractEventLoop):
        while not future.cancelled():
            rclpy.spin_once(node)
        if not future.cancelled():
            event_loop.call_soon_threadsafe(future.set_result, None)
    event_loop = asyncio.get_event_loop()
    spin_task = event_loop.create_future()
    spin_thread = threading.Thread(target=_spin, args=(node, spin_task, event_loop))
    spin_thread.start()
    try:
        await spin_task
    except asyncio.CancelledError:
        cancel.trigger()
    spin_thread.join()
    node.destroy_guard_condition(cancel)


async def start_node():
    base_node = RobotBaseNode()
    spin_task = asyncio.get_event_loop().create_task(spin(base_node))
    sleep_task_lst = []
    
    for i in range(len(base_node.robot_ip_lst)):
        
        conn = Go2Connection(
        robot_ip=base_node.robot_ip_lst[i],
        robot_num=i,
        token=base_node.token,
        on_validated=base_node.on_validated,
        on_message=base_node.on_data_channel_message,
        )
        
        sleep_task_lst.append(asyncio.get_event_loop().create_task(base_node.run(conn, i)))

    await asyncio.wait([spin_task, *sleep_task_lst], return_when=asyncio.FIRST_COMPLETED)

def main():
    rclpy.init()
    asyncio.get_event_loop().run_until_complete(start_node())
    asyncio.get_event_loop().close()
    rclpy.shutdown()

if __name__ == '__main__':
    main()