# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from lisa.executable import Tool


class Whoami(Tool):
    @property
    def command(self) -> str:
        return "whoami"

    @property
    def can_install(self) -> bool:
        return False
