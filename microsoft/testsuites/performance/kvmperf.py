# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import inspect
import time
from typing import Any, Dict, List

from lisa import (
    TestCaseMetadata,
    TestSuite,
    TestSuiteMetadata,
    notifier,
    schema,
    search_space,
    simple_requirement,
)
from lisa.environment import Environment
from lisa.features import Disk
from lisa.messages import DiskSetupType, DiskType
from lisa.node import RemoteNode
from lisa.tools import Ip, Lagscope, Lscpu, Ntttcp
from lisa.tools.ntttcp import NTTTCP_TCP_CONCURRENCY
from microsoft.testsuites.nested.common import (
    connect_nested_vm,
    parse_nested_image_variables,
)
from microsoft.testsuites.performance.common import (
    reset_partitions,
    reset_raid,
    run_perf_test,
    stop_raid,
)


@TestSuiteMetadata(
    area="storage",
    category="performance",
    description="""
    This test suite is to validate performance of nested VM using FIO tool.
    """,
)
class KVMPerformance(TestSuite):  # noqa
    TIME_OUT = 12000

    CLIENT_IMAGE = "nestedclient.qcow2"
    SERVER_IMAGE = "nestedserver.qcow2"
    CLIENT_HOST_FWD_PORT = 60022
    SERVER_HOST_FWD_PORT = 60023
    BR_NAME = "br0"
    BR_ADDR = "192.168.1.10"
    CLIENT_IP_ADDR = "192.168.1.14"
    SERVER_IP_ADDR = "192.168.1.15"
    CLIENT_TAP = "tap1"
    SERVER_TAP = "tap2"
    NIC_NAME = "ens4"

    @TestCaseMetadata(
        description="""
        This test case is to validate performance of nested VM using fio tool
        with single l1 data disk attached to the l2 VM.
        """,
        priority=3,
        timeout=TIME_OUT,
        requirement=simple_requirement(
            disk=schema.DiskOptionSettings(
                disk_type=schema.DiskType.PremiumSSDLRS,
                data_disk_iops=search_space.IntRange(min=5000),
                data_disk_count=search_space.IntRange(min=1),
            ),
        ),
    )
    def perf_nested_kvm_storage_singledisk(
        self, node: RemoteNode, environment: Environment, variables: Dict[str, Any]
    ) -> None:
        self._storage_perf_qemu(node, environment, variables, setup_raid=False)

    @TestCaseMetadata(
        description="""
        This test case is to validate performance of nested VM using fio tool with raid0
        configuratrion of 6 l1 data disk attached to the l2 VM.
        """,
        priority=3,
        timeout=TIME_OUT,
        requirement=simple_requirement(
            disk=schema.DiskOptionSettings(
                disk_type=schema.DiskType.PremiumSSDLRS,
                data_disk_iops=search_space.IntRange(min=5000),
                data_disk_count=search_space.IntRange(min=6),
            ),
        ),
    )
    def perf_nested_kvm_storage_multidisk(
        self, node: RemoteNode, environment: Environment, variables: Dict[str, Any]
    ) -> None:
        self._storage_perf_qemu(node, environment, variables)

    @TestCaseMetadata(
        description="""
        This test case runs ntttcp test on two nested VMs on same L1 guest
        connected with private bridge
        """,
        priority=3,
        timeout=TIME_OUT,
    )
    def perf_nested_kvm_ntttcp_private_bridge(
        self, node: RemoteNode, environment: Environment, variables: Dict[str, Any]
    ) -> None:
        self._ntttcp_perf_qemu(node, environment, variables)

    def _ntttcp_perf_qemu(
        self,
        node: RemoteNode,
        environment: Environment,
        variables: Dict[str, Any],
    ) -> None:
        (
            nested_image_username,
            nested_image_password,
            _,
            nested_image_url,
        ) = parse_nested_image_variables(variables)

        # setup bridge and taps
        node.tools[Ip].setup_bridge(self.BR_NAME, self.BR_ADDR)
        node.tools[Ip].setup_tap(self.CLIENT_TAP, self.BR_NAME)
        node.tools[Ip].setup_tap(self.SERVER_TAP, self.BR_NAME)

        # setup client and server
        client = connect_nested_vm(
            node,
            nested_image_username,
            nested_image_password,
            self.CLIENT_HOST_FWD_PORT,
            nested_image_url,
            image_name=self.CLIENT_IMAGE,
            nic_model="virtio-net-pci",
            taps=[self.CLIENT_TAP],
            name="client",
        )
        client.tools[Ip].addr_add(self.NIC_NAME, self.CLIENT_IP_ADDR)
        client.tools[Ip].up(self.NIC_NAME)

        server = connect_nested_vm(
            node,
            nested_image_username,
            nested_image_password,
            self.SERVER_HOST_FWD_PORT,
            nested_image_url,
            image_name=self.SERVER_IMAGE,
            stop_existing_vm=False,
            nic_model="virtio-net-pci",
            taps=[self.SERVER_TAP],
            name="server",
        )
        server.tools[Ip].addr_add(self.NIC_NAME, self.SERVER_IP_ADDR)
        server.tools[Ip].up(self.NIC_NAME)

        # run ntttcp test
        client_ntttcp = client.tools[Ntttcp]
        server_ntttcp = server.tools[Ntttcp]
        client_lagscope = client.tools[Lagscope]
        server_lagscope = server.tools[Lagscope]
        for ntttcp in [client_ntttcp, server_ntttcp]:
            ntttcp.set_sys_variables(udp_mode=False)
            ntttcp.set_tasks_max()
        server_nic_name = self.NIC_NAME
        client_nic_name = self.NIC_NAME

        server_lagscope.run_as_server(ip=self.SERVER_IP_ADDR)
        max_server_threads = 64
        perf_ntttcp_message_list: List[Any] = []
        for test_thread in NTTTCP_TCP_CONCURRENCY:
            if test_thread < max_server_threads:
                num_threads_p = test_thread
                num_threads_n = 1
            else:
                num_threads_p = max_server_threads
                num_threads_n = int(test_thread / num_threads_p)
            if 1 == num_threads_n and 1 == num_threads_p:
                buffer_size = int(1048576 / 1024)
            else:
                buffer_size = int(65536 / 1024)
            server_result = server_ntttcp.run_as_server_async(
                server_nic_name,
                ports_count=num_threads_p,
                buffer_size=buffer_size,
                dev_differentiator="Hypervisor callback interrupts",
                udp_mode=False,
            )
            client_lagscope_process = client_lagscope.run_as_client_async(
                server_ip=self.SERVER_IP_ADDR,
                ping_count=0,
                run_time_seconds=10,
                print_histogram=False,
                print_percentile=False,
                histogram_1st_interval_start_value=0,
                length_of_histogram_intervals=0,
                count_of_histogram_intervals=0,
                dump_csv=False,
            )
            client_ntttcp_result = client_ntttcp.run_as_client(
                client_nic_name,
                self.SERVER_IP_ADDR,
                buffer_size=buffer_size,
                threads_count=num_threads_n,
                ports_count=num_threads_p,
                dev_differentiator="Hypervisor callback interrupts",
                udp_mode=False,
            )
            server_ntttcp_result = server_result.wait_result()
            server_result_temp = server_ntttcp.create_ntttcp_result(
                server_ntttcp_result
            )
            client_result_temp = client_ntttcp.create_ntttcp_result(
                client_ntttcp_result, role="client"
            )
            client_sar_result = client_lagscope_process.wait_result()
            client_average_latency = client_lagscope.get_average(client_sar_result)

            perf_ntttcp_message_list.append(
                client_ntttcp.create_ntttcp_tcp_performance_message(
                    server_result_temp,
                    client_result_temp,
                    client_average_latency,
                    str(test_thread),
                    buffer_size,
                    environment,
                    inspect.stack()[1][3],
                )
            )

        for ntttcp_message in perf_ntttcp_message_list:
            notifier.notify(ntttcp_message)

    def _storage_perf_qemu(
        self,
        node: RemoteNode,
        environment: Environment,
        variables: Dict[str, Any],
        filename: str = "/dev/sdb",
        start_iodepth: int = 1,
        max_iodepth: int = 1024,
        setup_raid: bool = True,
    ) -> None:
        (
            nested_image_username,
            nested_image_password,
            nested_image_port,
            nested_image_url,
        ) = parse_nested_image_variables(variables)

        l1_data_disks = node.features[Disk].get_raw_data_disks()
        l1_data_disk_count = len(l1_data_disks)

        # setup raid on l1 data disks
        if setup_raid:
            disks = ["md0"]
            l1_partition_disks = reset_partitions(node, l1_data_disks)
            stop_raid(node)
            reset_raid(node, l1_partition_disks)
        else:
            disks = ["sdb"]

        # get l2 vm
        l2_vm = connect_nested_vm(
            node,
            nested_image_username,
            nested_image_password,
            nested_image_port,
            nested_image_url,
            disks=disks,
        )

        # Qemu command exits immediately but the VM requires some time to boot up.
        time.sleep(60)
        l2_vm.tools[Lscpu].get_core_count()

        # Each fio process start jobs equal to the iodepth to read/write from
        # the disks. The max number of jobs can be equal to the core count of
        # the node.
        # Examples:
        # iodepth = 4, core count = 8 => max_jobs = 4
        # iodepth = 16, core count = 8 => max_jobs = 8
        num_jobs = []
        iodepth_iter = start_iodepth
        core_count = node.tools[Lscpu].get_core_count()
        while iodepth_iter <= max_iodepth:
            num_jobs.append(min(iodepth_iter, core_count))
            iodepth_iter = iodepth_iter * 2

        # run fio test
        run_perf_test(
            l2_vm,
            start_iodepth,
            max_iodepth,
            filename,
            test_name=inspect.stack()[1][3],
            core_count=core_count,
            disk_count=l1_data_disk_count,
            disk_setup_type=DiskSetupType.raid0,
            disk_type=DiskType.premiumssd,
            environment=environment,
            num_jobs=num_jobs,
            size_gb=8,
            overwrite=True,
        )
