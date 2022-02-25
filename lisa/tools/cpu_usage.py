from dataclasses import dataclass
from decimal import Decimal
from typing import Any, List, Type, cast

import numpy

from lisa.executable import Tool
from lisa.operating_system import Debian, Posix, Redhat, Suse
from lisa.tools.lscpu import Lscpu
from lisa.util import LisaException


@dataclass
class CpuUsageStats:
    # All cpu usage values are normalized values
    avg_system_cpu = Decimal(0)
    min_system_cpu = Decimal(0)
    max_system_cpu = Decimal(0)
    med_system_cpu = Decimal(0)
    num_vcpus: int = 0


class CpuUsage(Tool):
    def __init__(self) -> None:
        self._usage_stats = CpuUsageStats()
        self._cpu_values = []  # type: List[float]

    @property
    def can_install(self) -> bool:
        return True

    @property
    def dependencies(self) -> List[Type[Tool]]:
        return []

    @property
    def command(self) -> str:
        return "mpstat"

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        pass

    def _install(self) -> bool:
        return self._install_dep_packages()

    def _install_dep_packages(self) -> bool:
        posix_os: Posix = cast(Posix, self.node.os)
        package_list = ["sysstat"]
        if (
            isinstance(self.node.os, Redhat)
            or isinstance(self.node.os, Debian)
            or isinstance(self.node.os, Suse)
        ):
            pass
        else:
            raise LisaException(
                f"tool {self.command} can't be installed in distro {self.node.os.name}."
            )
        for package in list(package_list):
            if posix_os.is_package_in_repo(package):
                posix_os.install_packages(package)
        return True

    def measure_cpu(self, num_secs: int) -> bool:
        # Issue mpstat
        cmd = f"mpstat -u 1 {num_secs}"

        result = self.run(
            cmd,
            shell=True,
            expected_exit_code=0,
            expected_exit_code_failure_message="Failed to run mpstat",
        )

        for line in result.stdout:
            line = line.rstrip()

            if len(line) == 0 or "CPU" in line or "Average" in line:
                continue

            try:
                split_line = line.split()
                cpu_idle = float(split_line[len(split_line) - 1])
                if cpu_idle < 100.0:
                    self._cpu_values.append(100.0 - cpu_idle)
            except ValueError:
                continue

        if len(self._cpu_values) == 0:
            return False

        self._usage_stats.min_system_cpu = Decimal(min(self._cpu_values))
        self._usage_stats.max_system_cpu = Decimal(max(self._cpu_values))
        self._usage_stats.avg_system_cpu = Decimal(
            repr(numpy.average(self._cpu_values))
        )
        self._usage_stats.med_system_cpu = Decimal(repr(numpy.median(self._cpu_values)))
        lscpu = self.node.tools[Lscpu]
        self._usage_stats.num_vcpus = lscpu.get_core_count()

        return True

    def get_stats(self) -> CpuUsageStats:
        return self._usage_stats
