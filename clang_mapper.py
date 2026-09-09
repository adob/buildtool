"""Synchronous JSON-line transport to the patched Clang frontend wrapper."""

import json
import os
import shlex
import shutil
import subprocess


def compile_with_mapper(wrapper, command, resolve):
    """Run one compile; resolve(request) returns a PCM path or raises.

    Each invocation owns its pipes, so resolve can recursively compile imports.
    The resolver is separate from process execution for in-memory unit tests.
    """
    command = list(map(str, command))
    # Clang's driver API uses argv[0] to locate its resource headers.
    command[0] = shutil.which(command[0]) or command[0]
    parent_read, child_write = os.pipe()
    child_read, parent_write = os.pipe()
    process = None
    try:
        wrapper_command = [str(wrapper), "--mapper-fds", str(child_read),
                           str(child_write), "--", *command]
        print(shlex.join(wrapper_command), flush=True)
        process = subprocess.Popen(
            wrapper_command,
            pass_fds=(child_read, child_write),
        )
    finally:
        os.close(child_read)
        os.close(child_write)
        if process is None:
            os.close(parent_read)
            os.close(parent_write)
    try:
        with os.fdopen(parent_read, "r") as requests, os.fdopen(parent_write, "w") as replies:
            for line in requests:
                try:
                    reply = {"pcm": str(resolve(json.loads(line)))}
                except BaseException as error:
                    # Includes SystemExit from existing buildtool failures.
                    # Wake the blocked child before propagating the failure.
                    try:
                        replies.write(json.dumps({"error": str(error)}) + "\n")
                        replies.flush()
                    except BrokenPipeError:
                        pass
                    raise
                replies.write(json.dumps(reply) + "\n")
                replies.flush()
        code = process.wait()
        if code:
            raise subprocess.CalledProcessError(code, command)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
