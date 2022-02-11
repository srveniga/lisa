# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import random
from typing import Any, List, Optional

from assertpy.assertpy import assert_that

from lisa.executable import Tool
from lisa.operating_system import Fedora, Posix
from lisa.tools import Kill, Lsmod


def generate_random_mac_address() -> str:
    return "02:00:00:%02x:%02x:%02x" % (
        random.randint(0, 255),
        random.randint(0, 255),
        random.randint(0, 255),
    )


class Qemu(Tool):

    QEMU_INSTALL_LOCATIONS = ["qemu-system-x86_64", "qemu-kvm", "/usr/libexec/qemu-kvm"]

    @property
    def command(self) -> str:
        return self._qemu_command

    def can_install(self) -> bool:
        return True

    def create_vm(
        self,
        port: int,
        guest_image_path: str,
        cores: int = 2,
        memory: int = 2048,
        nics: int = 1,
        nic_model: str = "e1000",
        taps: Optional[List[str]] = None,
        disks: Optional[List[str]] = None,
        stop_existing_vm: bool = True,
    ) -> None:
        # start vm on the current node
        # port : port of the host vm mapped to the guest's ssh port
        # guest_image_path : path of the guest image

        # Run qemu with following parameters:
        # -m: memory size
        # -smp: SMP system with `n` CPUs
        # -hda : guest image path
        cmd = f"-smp {cores} -m {memory} -hda {guest_image_path} "

        # add devices
        for i in range(nics):
            random_mac_address = generate_random_mac_address()
            cmd += f"-device {nic_model},netdev=net{i},mac={random_mac_address} "
            cmd += f"-netdev user,id=net{i},hostfwd=tcp::{port}-:22 "

        # add tap devices
        if taps:
            for tap in taps:
                random_mac_address = generate_random_mac_address()
                cmd += (
                    f"-device {nic_model},netdev=nettap{i},"
                    f"mac={random_mac_address},mq=on,vectors=10 "
                )
                cmd += (
                    f"-netdev tap,id=nettap{i},"
                    f"ifname={tap},script=no,vhost=on,queues=4 "
                )

        # add disks
        if disks:
            for disk in disks:
                cmd += (
                    f"-drive id=datadisk-{disk},"
                    f"file=/dev/{disk},cache=none,if=none,format=raw,aio=threads "
                    f"-device virtio-scsi-pci -device scsi-hd,drive=datadisk-{disk} "
                )

        # -enable-kvm: enable kvm
        # -display: enable or disable display
        # -demonize: run in background
        cmd += "-enable-kvm -display none -daemonize "

        # kill any existing qemu process if stop_existing_vm is True
        if stop_existing_vm:
            self.stop_vm()

        self.run(
            cmd,
            sudo=True,
            shell=True,
            expected_exit_code=0,
            expected_exit_code_failure_message=f"Unable to start VM {guest_image_path}",
        )

        # update firewall rules
        # https://access.redhat.com/documentation/en-us/red_hat_enterprise_linux/8/html/configuring_and_managing_networking/using-and-configuring-firewalld_configuring-and-managing-networking # noqa E501
        if isinstance(self.node.os, Fedora):
            self.node.execute(
                f"firewall-cmd --permanent --add-port={port}/tcp", sudo=True
            )
            self.node.execute("firewall-cmd --reload", sudo=True)

    def stop_vm(self) -> None:
        # stop vm
        kill = self.node.tools[Kill]
        kill.by_name("qemu")

    def _initialize(self, *args: Any, **kwargs: Any) -> None:
        self._qemu_command = "qemu-system-x86_64"

    def _install(self) -> bool:
        assert isinstance(self.node.os, Posix)

        # install qemu
        self.node.os.install_packages("qemu-kvm")

        # verify that kvm is enabled
        self._is_kvm_successfully_enabled()

        # find correct command for qemu
        for location in self.QEMU_INSTALL_LOCATIONS:
            self._qemu_command = location
            if self._check_exists():
                return True

        return False

    def _is_kvm_successfully_enabled(self) -> None:
        # verify that kvm module is loaded
        lsmod_output = self.node.tools[Lsmod].run().stdout
        is_kvm_successfully_enabled = (
            "kvm_intel" in lsmod_output or "kvm_amd" in lsmod_output
        )
        assert_that(
            is_kvm_successfully_enabled, f"KVM could not be enabled : {lsmod_output}"
        ).is_true()
