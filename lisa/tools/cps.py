# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Tuple, Type

import numpy
from scipy import stats
from statsmodels import robust

from lisa.executable import Tool
from lisa.messages import CpsPerformanceMessage, create_message
from lisa.tools import Firewall, Lspci, Wget
from lisa.util import constants
from lisa.util.process import ExecutableResult, Process

if TYPE_CHECKING:
    from lisa.environment import Environment


@dataclass
class CpsResult:
    min: Decimal = Decimal(0)
    max: Decimal = Decimal(0)
    med: Decimal = Decimal(0)
    avg: Decimal = Decimal(0)
    percentile_99_999: Decimal = Decimal(0)
    percentile_99_99: Decimal = Decimal(0)
    percentile_99_9: Decimal = Decimal(0)
    percentile_99: Decimal = Decimal(0)
    percentile_90: Decimal = Decimal(0)
    percentile_75: Decimal = Decimal(0)
    percentile_50: Decimal = Decimal(0)
    percentile_25: Decimal = Decimal(0)
    mad: Decimal = Decimal(0)
    std_err: Decimal = Decimal(0)
    lower_ci: Decimal = Decimal(0)
    upper_ci: Decimal = Decimal(0)
    ci_significance_level: Decimal = Decimal(0)
    min_cpu: Decimal = Decimal(0)
    max_cpu: Decimal = Decimal(0)
    med_cpu: Decimal = Decimal(0)
    avg_cpu: Decimal = Decimal(0)
    duration_in_secs: Decimal = Decimal(0)
    command: str = ""
    accel_net: str = ""
    num_threads: int = 0


class Cps(Tool):
    def __init__(self, tool_remote_location: str) -> None:
        self._server_result_file = Path("/tmp/cps_result.txt")
        self._client_result_file = Path("/tmp/cps_result.txt")
        self._results_per_sec = []  # type: List[float]
        self._cps_result = CpsResult()
        self._tool_remote_location = tool_remote_location
        self._tool_path = f"/home/{constants.DEFAULT_USER_NAME}/"

    @property
    def dependencies(self) -> List[Type[Tool]]:
        return [Lspci, Firewall, Wget]

    @property
    def command(self) -> str:
        return "cps"

    @property
    def can_install(self) -> bool:
        return True

    def help(self) -> ExecutableResult:
        return self.run("-h")

    def input_args_okay(self, run_time_seconds: int, warm_up_time_seconds: int) -> bool:
        if run_time_seconds <= warm_up_time_seconds:
            return True
        return False

    def process_additional_stats_from_results(
        self, significance_level: Decimal
    ) -> None:
        self._cps_result.min = Decimal(min(self._results_per_sec))
        self._cps_result.max = Decimal(max(self._results_per_sec))
        self._cps_result.avg = Decimal(repr(numpy.average(self._results_per_sec)))
        self._cps_result.med = Decimal(repr(numpy.median(self._results_per_sec)))
        self._cps_result.ci_significance_level = significance_level
        self._cps_result.mad = robust.mad(self._results_per_sec)

        sem = stats.sem(self._results_per_sec)
        self._cps_result.std_err = sem

        t_interval = stats.t.interval(
            (significance_level / 100),
            len(self._results_per_sec) - 1,
            loc=numpy.mean(self._results_per_sec),
            scale=sem,
        )
        self._cps_result.lower_ci = t_interval[0]
        self._cps_result.upper_ci = t_interval[1]

        self._cps_result.percentile_25 = Decimal(
            repr(numpy.percentile(self._results_per_sec, 25))
        )
        self._cps_result.percentile_50 = Decimal(
            repr(numpy.percentile(self._results_per_sec, 50))
        )
        self._cps_result.percentile_75 = Decimal(
            repr(numpy.percentile(self._results_per_sec, 75))
        )
        self._cps_result.percentile_90 = Decimal(
            repr(numpy.percentile(self._results_per_sec, 90))
        )
        self._cps_result.percentile_99 = Decimal(
            repr(numpy.percentile(self._results_per_sec, 99))
        )
        self._cps_result.percentile_99_9 = Decimal(
            repr(numpy.percentile(self._results_per_sec, 99.9))
        )
        self._cps_result.percentile_99_99 = Decimal(
            repr(numpy.percentile(self._results_per_sec, 99.99))
        )
        self._cps_result.percentile_99_999 = Decimal(
            repr(numpy.percentile(self._results_per_sec, 99.999))
        )

    def process_result_file(
        self, warmup_time_secs: int, significance_level: Decimal, result_file: Path
    ) -> Tuple[CpsResult, int]:
        assert result_file.exists()

        # cps reporting requires data samples during the
        # warmup period to be excluded from data set
        exclude_count = warmup_time_secs
        append_result = True
        fd = open(result_file.absolute(), "r")
        lines = fd.readlines()
        processed_results = 0

        for line in lines:
            line.strip("\n")
            if line == "":
                continue
            elif append_result is True:
                try:
                    string_list = line.split()
                    if "Conn/s" in string_list:
                        value_index = string_list.index("Conn/s")
                        continue
                    elif len(string_list) == 0:
                        append_result = False
                        continue
                    else:
                        if exclude_count > 0:
                            exclude_count -= 1
                        else:
                            result = float(string_list[value_index])
                            processed_results += 1
                            self._results_per_sec.append(result)
                except ValueError:
                    continue

        fd.close()

        # compute and collect the derived stats
        self.process_additional_stats_from_results(significance_level)
        return self._cps_result, processed_results

    def process_server_output(self, cmd_result: ExecutableResult) -> CpsResult:
        pass

    def run_as_server_async(
        self,
        run_time_seconds: int = 330,
        warm_up_time_seconds: int = 30,
        num_threads: int = 16,
        server_port: int = 8001,
    ) -> Process:
        cmd = ""
        cmd += (
            f" -s -r {num_threads} 0.0.0.0,{server_port} -t {run_time_seconds}"
            f" -wt {warm_up_time_seconds} "
        )

        process = self.node.execute_async(
            f"ulimit -n 1048575 && {self.command} {cmd}", shell=True, sudo=True
        )
        return process

    def wait_for_server_result(
        self,
        warm_up_time_seconds: int,
        significance_level: Decimal,
        server_process: Process,
    ) -> Tuple[CpsResult, int]:
        server_process.wait_result(
            expected_exit_code=0,
            expected_exit_code_failure_message="fail to launch cps server",
        )
        server_cps_result, num_data_values = self.process_result_file(
            warm_up_time_seconds, significance_level, self._server_result_file
        )
        return server_cps_result, num_data_values

    def run_as_client_async(
        self,
        local_ip: str,
        local_port: int,
        server_ip: str,
        server_port: int,
        threads_count: int,
        num_conns_per_thread: int,
        max_pending_connects_per_thread: int,
        connection_duration_ms: int = 0,
        data_transfer_mode: int = 0,
        run_time_seconds: int = 330,
        warm_up_time_seconds: int = 30,
        significance_level: Decimal = Decimal(99),
    ) -> Process:
        # -c: run as a client which establishes connections to
        #     specified remote addresses/ports
        # -r: repeat option for creating multiple with same params
        #     num threads <local ip, local port, remote ip, remote
        #     port, N, P, D, M> where
        #     N: total number of conns to open for the thread
        #     P: max num of pending connect requests at any given time
        #        for the thread
        #     D: duration in ms for each connection established by
        #        the thread
        #     M: data transfer mode for the thread; 0: no send/receive
        #        1: one send/receive
        #        2: continuous send/receive
        # -t: Time of test duration in seconds [default: run forever]
        # -wt: skip these many seconds when reporting the final stats
        #      at the end [default: 0]
        cmd = (
            f" -c -r {threads_count} {local_ip},{local_port},{server_ip},{server_port},"
            f"{num_conns_per_thread},{max_pending_connects_per_thread},"
            f"{connection_duration_ms},{data_transfer_mode}"
            f" -t {run_time_seconds} -wt {warm_up_time_seconds}  "
        )
        process = self.node.execute_async(
            f"ulimit -n 1048575 && {self.command} {cmd}", shell=True, sudo=True
        )
        return process

    def wait_for_client_result(
        self,
        warm_up_time_seconds: int,
        significance_level: Decimal,
        client_process: Process,
    ) -> Tuple[CpsResult, int]:
        client_process.wait_result(
            expected_exit_code=0,
            expected_exit_code_failure_message="fail to launch cps client",
        )

        client_cps_result, num_data_values = self.process_result_file(
            warm_up_time_seconds, significance_level, self._client_result_file
        )
        return client_cps_result, num_data_values

    def create_cps_performance_message(
        self,
        server_result: CpsResult,
        client_result: CpsResult,
        environment: "Environment",
        test_case_name: str,
    ) -> CpsPerformanceMessage:
        other_fields: Dict[str, Any] = {}
        other_fields["tool"] = constants.NETWORK_PERFORMANCE_TOOL_CPS
        other_fields["min"] = server_result.min
        other_fields["max"] = server_result.max
        other_fields["med"] = server_result.med
        other_fields["avg"] = server_result.avg
        other_fields["percentile_99_999"] = server_result.percentile_99_999
        other_fields["percentile_99_99"] = server_result.percentile_99_99
        other_fields["percentile_99_9"] = server_result.percentile_99_9
        other_fields["percentile_99"] = server_result.percentile_99
        other_fields["percentile_90"] = server_result.percentile_90
        other_fields["percentile_75"] = server_result.percentile_75
        other_fields["percentile_50"] = server_result.percentile_50
        other_fields["percentile_25"] = server_result.percentile_25
        other_fields["mad"] = server_result.mad
        other_fields["std_err"] = server_result.std_err
        other_fields["lower_ci"] = server_result.lower_ci
        other_fields["upper_ci"] = server_result.upper_ci
        other_fields["ci_significance_level"] = server_result.ci_significance_level
        other_fields["min_cpu"] = server_result.cycles_per_byte
        other_fields["max_cpu"] = server_result.cycles_per_byte
        other_fields["med_cpu"] = server_result.cycles_per_byte
        other_fields["avg_cpu"] = server_result.cycles_per_byte
        other_fields["duration_in_secs"] = server_result.duration_in_secs
        other_fields["num_threads"] = server_result.num_threads
        other_fields["command"] = client_result.command
        other_fields["accel_net"] = (
            f"client:{client_result.accel_net}" f"server:{server_result.accel_net}"
        )

        return create_message(
            CpsPerformanceMessage, self.node, environment, test_case_name, other_fields
        )

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        firewall = self.node.tools[Firewall]
        firewall.stop()

    def _install(self) -> bool:
        # copy the binary from the location to home folder
        wget = self.node.tools[Wget]
        wget.get(
            self._tool_remote_location,
            file_path=self._tool_path,
            filename=constants.NETWORK_PERFORMANCE_TOOL_CPS,
            executable=True,
        )
        cps_bin = f"{self._tool_path}{constants.NETWORK_PERFORMANCE_TOOL_CPS}"
        chmod_cmd = f"chmod +x {cps_bin}"

        result = self.node.execute(chmod_cmd, sudo=True, cwd=Path(cps_bin))
        result.assert_exit_code()

        if result.exit_code == 0:
            return True
        else:
            return False
