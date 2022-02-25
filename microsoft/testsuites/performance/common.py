# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
import inspect
import pathlib
from typing import Any, Dict, List, Optional, Union, cast

from lisa import Node, RemoteNode, notifier, run_in_parallel
from lisa.environment import Environment
from lisa.messages import (
    CpsPerformanceMessage,
    DiskPerformanceMessage,
    DiskSetupType,
    DiskType,
    NetworkLatencyPerformanceMessage,
    NetworkTCPPerformanceMessage,
    NetworkUDPPerformanceMessage,
)
from lisa.schema import NetworkDataPath
from lisa.tools import (
    FIOMODES,
    Cps,
    CpuUsage,
    Fdisk,
    Fio,
    FIOResult,
    Iperf3,
    Kill,
    Lagscope,
    Lscpu,
    Mdadm,
    Netperf,
    Ntttcp,
    Sar,
    Ssh,
)
from lisa.tools.ntttcp import NTTTCP_TCP_CONCURRENCY, NTTTCP_UDP_CONCURRENCY
from lisa.util.process import ExecutableResult, Process


def perf_disk(
    node: Node,
    start_iodepth: int,
    max_iodepth: int,
    filename: str,
    core_count: int,
    disk_count: int,
    disk_setup_type: DiskSetupType,
    disk_type: DiskType,
    environment: Environment,
    test_name: str = "",
    num_jobs: Optional[List[int]] = None,
    block_size: int = 4,
    time: int = 120,
    size_gb: int = 0,
    numjob: int = 0,
    overwrite: bool = False,
    cwd: Optional[pathlib.PurePath] = None,
) -> None:
    fio_result_list: List[FIOResult] = []
    fio = node.tools[Fio]
    numjobiterator = 0
    for mode in FIOMODES:
        iodepth = start_iodepth
        numjobindex = 0
        while iodepth <= max_iodepth:
            if num_jobs:
                numjob = num_jobs[numjobindex]
            fio_result = fio.launch(
                name=f"iteration{numjobiterator}",
                filename=filename,
                mode=mode.name,
                time=time,
                size_gb=size_gb,
                block_size=f"{block_size}K",
                iodepth=iodepth,
                overwrite=overwrite,
                numjob=numjob,
                cwd=cwd,
            )
            fio_result_list.append(fio_result)
            iodepth = iodepth * 2
            numjobindex += 1
            numjobiterator += 1

    other_fields: Dict[str, Any] = {}
    other_fields["core_count"] = core_count
    other_fields["disk_count"] = disk_count
    other_fields["block_size"] = block_size
    other_fields["disk_setup_type"] = disk_setup_type
    other_fields["disk_type"] = disk_type
    if not test_name:
        test_name = inspect.stack()[1][3]
    fio_messages: List[DiskPerformanceMessage] = fio.create_performance_messages(
        fio_result_list,
        test_name=test_name,
        environment=environment,
        other_fields=other_fields,
    )
    for fio_message in fio_messages:
        notifier.notify(fio_message)


def get_nic_datapath(node: Node) -> str:
    data_path: str = ""
    assert (
        node.capability.network_interface
        and node.capability.network_interface.data_path
    )
    if isinstance(node.capability.network_interface.data_path, NetworkDataPath):
        data_path = node.capability.network_interface.data_path.value
    return data_path


def cleanup_process(environment: Environment, process_name: str) -> None:
    for node in environment.nodes.list():
        kill = node.tools[Kill]
        kill.by_name(process_name)


def reset_partitions(
    node: Node,
    disk_names: List[str],
) -> List[str]:
    fdisk = node.tools[Fdisk]
    partition_disks: List[str] = []
    for data_disk in disk_names:
        fdisk.delete_partitions(data_disk)
        partition_disks.append(fdisk.make_partition(data_disk, format=False))
    return partition_disks


def stop_raid(node: Node) -> None:
    mdadm = node.tools[Mdadm]
    mdadm.stop_raid()


def reset_raid(node: Node, disk_list: List[str]) -> None:
    stop_raid(node)
    mdadm = node.tools[Mdadm]
    mdadm.create_raid(disk_list)


def perf_tcp_latency(
    environment: Environment,
) -> List[NetworkLatencyPerformanceMessage]:
    client = cast(RemoteNode, environment.nodes[0])
    server = cast(RemoteNode, environment.nodes[1])
    client_lagscope = client.tools[Lagscope]
    server_lagscope = server.tools[Lagscope]
    try:
        for lagscope in [client_lagscope, server_lagscope]:
            lagscope.set_busy_poll()
        server_lagscope.run_as_server(ip=server.internal_address)
        latency_perf_messages = client_lagscope.create_latency_peformance_messages(
            client_lagscope.run_as_client(server_ip=server.internal_address),
            environment,
            inspect.stack()[1][3],
        )
    finally:
        for lagscope in [client_lagscope, server_lagscope]:
            lagscope.restore_busy_poll()

    return latency_perf_messages


def perf_tcp_pps(environment: Environment, test_type: str) -> None:
    client = cast(RemoteNode, environment.nodes[0])
    server = cast(RemoteNode, environment.nodes[1])
    client_netperf = client.tools[Netperf]
    server_netperf = server.tools[Netperf]

    cpu = client.tools[Lscpu]
    core_count = cpu.get_core_count()
    if "maxpps" == test_type:
        ssh = client.tools[Ssh]
        ssh.set_max_session()
        client.close()
        ports = range(30000, 30032)
    else:
        ports = range(30000, 30001)
    for port in ports:
        server_netperf.run_as_server(port)
    for port in ports:
        client_netperf.run_as_client_async(server.internal_address, core_count, port)
    client_sar = client.tools[Sar]
    server_sar = server.tools[Sar]
    server_sar.get_statistics_async()
    result = client_sar.get_statistics()
    pps_message = client_sar.create_pps_peformance_messages(
        result, inspect.stack()[1][3], environment, test_type
    )
    notifier.notify(pps_message)


def perf_ntttcp(
    environment: Environment,
    udp_mode: bool = False,
    connections: Optional[List[int]] = None,
    test_case_name: str = "",
) -> List[Union[NetworkTCPPerformanceMessage, NetworkUDPPerformanceMessage]]:
    client = cast(RemoteNode, environment.nodes[0])
    server = cast(RemoteNode, environment.nodes[1])
    if not test_case_name:
        # if it's not filled, assume it's called by case directly.
        test_case_name = inspect.stack()[1][3]
    if connections is None:
        if udp_mode:
            connections = NTTTCP_UDP_CONCURRENCY
        else:
            connections = NTTTCP_TCP_CONCURRENCY

    client_ntttcp, server_ntttcp = run_in_parallel(
        [lambda: client.tools[Ntttcp], lambda: server.tools[Ntttcp]]
    )
    try:
        client_lagscope, server_lagscope = run_in_parallel(
            [lambda: client.tools[Lagscope], lambda: server.tools[Lagscope]]
        )

        data_path = get_nic_datapath(client)
        server_nic_name = server.nics.default_nic
        client_nic_name = client.nics.default_nic
        dev_differentiator = "Hypervisor callback interrupts"
        if NetworkDataPath.Sriov.value == data_path:
            server_nic_name = server.nics.get_lower_nics()[0]
            client_nic_name = client.nics.get_lower_nics()[0]
            dev_differentiator = "mlx"
        server_lagscope.run_as_server(ip=server.internal_address)
        max_server_threads = 64
        perf_ntttcp_message_list: List[
            Union[NetworkTCPPerformanceMessage, NetworkUDPPerformanceMessage]
        ] = []
        for test_thread in connections:
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
            if udp_mode:
                buffer_size = int(1024 / 1024)
            server_result = server_ntttcp.run_as_server_async(
                server_nic_name,
                ports_count=num_threads_p,
                buffer_size=buffer_size,
                dev_differentiator=dev_differentiator,
                udp_mode=udp_mode,
            )
            client_lagscope_process = client_lagscope.run_as_client_async(
                server_ip=server.internal_address,
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
                server.internal_address,
                buffer_size=buffer_size,
                threads_count=num_threads_n,
                ports_count=num_threads_p,
                dev_differentiator=dev_differentiator,
                udp_mode=udp_mode,
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
            if udp_mode:
                ntttcp_message: Union[
                    NetworkTCPPerformanceMessage, NetworkUDPPerformanceMessage
                ] = client_ntttcp.create_ntttcp_udp_performance_message(
                    server_result_temp,
                    client_result_temp,
                    str(test_thread),
                    buffer_size,
                    environment,
                    test_case_name,
                )
            else:
                ntttcp_message = client_ntttcp.create_ntttcp_tcp_performance_message(
                    server_result_temp,
                    client_result_temp,
                    client_average_latency,
                    str(test_thread),
                    buffer_size,
                    environment,
                    test_case_name,
                )
            notifier.notify(ntttcp_message)

            perf_ntttcp_message_list.append(ntttcp_message)
    finally:
        for ntttcp in [client_ntttcp, server_ntttcp]:
            ntttcp.restore_system(udp_mode)
        for lagscope in [client_lagscope, server_lagscope]:
            lagscope.restore_busy_poll()
    return perf_ntttcp_message_list


def perf_iperf(
    environment: Environment,
    connections: List[int],
    buffer_length_list: List[int],
    udp_mode: bool = False,
) -> None:
    client = cast(RemoteNode, environment.nodes[0])
    server = cast(RemoteNode, environment.nodes[1])
    client_iperf3 = client.tools[Iperf3]
    server_iperf3 = server.tools[Iperf3]
    test_case_name = inspect.stack()[1][3]
    iperf3_messages_list: List[Any] = []
    if udp_mode:
        for node in [client, server]:
            ssh = node.tools[Ssh]
            ssh.set_max_session()
            node.close()
    for buffer_length in buffer_length_list:
        for connection in connections:
            server_iperf3_process_list: List[Process] = []
            client_iperf3_process_list: List[Process] = []
            client_result_list: List[ExecutableResult] = []
            server_result_list: List[ExecutableResult] = []
            if connection < 64:
                num_threads_p = connection
                num_threads_n = 1
            else:
                num_threads_p = 64
                num_threads_n = int(connection / 64)
            server_start_port = 750
            current_server_port = server_start_port
            current_server_iperf_instances = 0
            while current_server_iperf_instances < num_threads_n:
                current_server_iperf_instances += 1
                server_iperf3_process_list.append(
                    server_iperf3.run_as_server_async(
                        current_server_port, "g", 10, True, True, False
                    )
                )
                current_server_port += 1
            client_start_port = 750
            current_client_port = client_start_port
            current_client_iperf_instances = 0
            while current_client_iperf_instances < num_threads_n:
                current_client_iperf_instances += 1
                client_iperf3_process_list.append(
                    client_iperf3.run_as_client_async(
                        server.internal_address,
                        output_json=True,
                        report_periodic=1,
                        report_unit="g",
                        port=current_client_port,
                        buffer_length=buffer_length,
                        run_time_seconds=10,
                        parallel_number=num_threads_p,
                        ip_version="4",
                        udp_mode=udp_mode,
                    )
                )
                current_client_port += 1
            for client_iperf3_process in client_iperf3_process_list:
                client_result_list.append(client_iperf3_process.wait_result())
            for server_iperf3_process in server_iperf3_process_list:
                server_result_list.append(server_iperf3_process.wait_result())
            if udp_mode:
                iperf3_messages_list.append(
                    client_iperf3.create_iperf_udp_performance_message(
                        server_result_list,
                        client_result_list,
                        buffer_length,
                        connection,
                        environment,
                        test_case_name,
                    )
                )
            else:
                iperf3_messages_list.append(
                    client_iperf3.create_iperf_tcp_performance_message(
                        server_result_list[0].stdout,
                        client_result_list[0].stdout,
                        buffer_length,
                        environment,
                        test_case_name,
                    )
                )
    for iperf3_message in iperf3_messages_list:
        notifier.notify(iperf3_message)


def calculate_middle_average(values: List[Union[float, int]]) -> float:
    """
    This method is used to calculate an average indicator. It discard the max
    and min value, and then take the average.
    """
    total = sum(x for x in values) - min(values) - max(values)
    # calculate average
    return total / (len(values) - 2)


def perf_cps(
    environment: Environment,
    num_threads: int = 16,
    test_case_name: str = "",
) -> List[CpsPerformanceMessage]:
    client = cast(RemoteNode, environment.nodes[0])
    server = cast(RemoteNode, environment.nodes[1])
    if not test_case_name:
        # if it's not filled, assume it's called by case directly.
        test_case_name = inspect.stack()[1][3]

    client_cps = client.tools[Cps]
    server_cps = server.tools[Cps]
    server_cpu = server.tools[CpuUsage]
    run_in_parallel(
        [lambda: client_cps.initialize("TODO"), lambda: server_cps.initialize("TODO")]
    )

    duration_secs = 330
    warmup_time_secs = 30
    server_process = server_cps.run_as_server_async(
        run_time_seconds=duration_secs,
        warm_up_time_seconds=warmup_time_secs,
        num_threads=num_threads,
        server_port=8001,
    )
    client_process = client_cps.run_as_client_async(
        local_ip="0.0.0.0",
        local_port=0,
        server_ip=server.internal_address,
        server_port=8001,
        threads_count=num_threads,
        num_conns_per_thread=100,
        max_pending_connects_per_thread=100,
        run_time_seconds=duration_secs,
        warm_up_time_seconds=warmup_time_secs,
    )
    if server_cpu.measure_cpu(duration_secs - 1):
        server_cpu_stats = server_cpu.get_stats()
    else:
        pass  # TODO: log the error

    client_result, client_num_observations = client_cps.wait_for_client_result(
        client_process
    )
    server_result, server_num_observations = server_cps.wait_for_server_result(
        server_process
    )

    # TODO: what to do when num_observations is < expected?
    cps_performance_msg = server_cps.create_cps_performance_message(
        server_result=server_result,
        client_result=client_result,
        environment=environment,
        test_case_name=test_case_name,
    )

    # Add the server CPU utilization
    cps_performance_msg.avg_cpu = server_cpu_stats.avg_system_cpu
    cps_performance_msg.min_cpu = server_cpu_stats.min_system_cpu
    cps_performance_msg.max_cpu = server_cpu_stats.max_system_cpu
    cps_performance_msg.med_cpu = server_cpu_stats.med_system_cpu

    notifier.notify(cps_performance_msg)
    perf_cps_message_list: List[CpsPerformanceMessage] = []
    perf_cps_message_list.append(cps_performance_msg)

    return perf_cps_message_list
