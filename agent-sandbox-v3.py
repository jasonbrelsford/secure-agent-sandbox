#!/usr/bin/env python3
"""
agent-sandbox-v3.py
Anthropic Research Engineer (Universes) Portfolio - Secure Sandbox Orchestrator
This script implements a secure orchestration layer using the Docker SDK to isolate,
execute, and monitor untrusted agentic code. It features unified Python logging at the INFO
level, detailed line-by-line tracing of executed payload outputs, and defense-in-depth security limits.
"""

import time
import sys
import os
import shutil
import tempfile
import logging
import base64
from typing import Dict, Any

# Configure structured python logging at the INFO level
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("SandboxOrchestrator")

# Conditional import for Docker SDK
try:
    import docker
    from docker.errors import DockerException, ContainerError, ImageNotFound
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False


class AgentSandboxOrchestrator:
    """
    A secure orchestration layer that uses Docker to run untrusted agentic code.
    Implements security boundaries (CPU/memory constraints, read-only root FS, 
    dropped capabilities, and complete network isolation).
    """
    def __init__(self, image_name: str = "python:3.11-slim", default_timeout: float = 5.0):
        self.image_name = image_name
        self.default_timeout = default_timeout
        self.client = None
        self._initialize_client()

    def _initialize_client(self) -> None:
        """Initializes the Docker client from the system environment."""
        if not DOCKER_AVAILABLE:
            return
        try:
            self.client = docker.from_env()
            # Ping the daemon to verify it's active and responsive
            self.client.ping()
        except Exception:
            self.client = None

    def is_ready(self) -> tuple[bool, str]:
        """
        Checks if the orchestration layer is fully ready to execute code.
        Returns (ready_status, error_message).
        """
        if not DOCKER_AVAILABLE:
            return False, (
                "The 'docker' Python package is not installed.\n"
                "Please run: pip install docker"
            )
        if self.client is None:
            return False, (
                "Cannot connect to the Docker Daemon.\n"
                "Please make sure Docker Desktop is running and accessible on your machine.\n"
                "For macOS/Linux, verify that the Docker socket is open and that your user "
                "has permission to read/write to it."
            )
        return True, ""

    def run_payload(self, code_payload: str, timeout: float = None) -> Dict[str, Any]:
        """
        Executes a string of Python code inside an isolated, hardened Docker container.
        Ensures that 'status', 'exit_code', 'output', and 'duration' are ALWAYS returned 
        to prevent downstream KeyErrors, while tracing container output line-by-line.
        """
        timeout = timeout or self.default_timeout
        start_time = time.time()

        # 1. Check Docker client readiness
        ready, error_msg = self.is_ready()
        if not ready:
            logger.error(f"Pre-flight readiness check failed: {error_msg}")
            return {
                "status": "error",
                "exit_code": -1,
                "output": f"Initialization Error: {error_msg}",
                "duration": round(time.time() - start_time, 3),
                "error": "Docker daemon connection failed."
            }

        temp_dir = tempfile.mkdtemp(prefix="agent_sandbox_")
        payload_file = os.path.join(temp_dir, "payload.py")

        try:
            # 2. Write code to a temporary file
            with open(payload_file, "w", encoding="utf-8") as f:
                f.write(code_payload)

            # 3. Ensure the base image is pulled locally
            try:
                self.client.images.get(self.image_name)
            except ImageNotFound:
                logger.info(f"Base image '{self.image_name}' not found. Pulling from registry...")
                self.client.images.pull(self.image_name)

            logger.info("Initializing secure sandbox container...")

            # 4. Set up security parameters
            container_config = {
                "image": self.image_name,
                "command": f"python -u /app/payload.py",
                "network_mode": "none",        # Complete network isolation
                "mem_limit": "128m",           # Absolute memory ceiling
                "cpu_period": 100000,
                "cpu_quota": 50000,            # Lock to 50% of a single core (0.5 CPU)
                "cap_drop": ["ALL"],           # Drop all kernel privileges
                "user": "1000:1000",           # Run as unprivileged non-root user
                "volumes": {
                    payload_file: {"bind": "/app/payload.py", "mode": "ro"},
                    temp_dir: {"bind": "/tmp", "mode": "rw"} # Isolated workspace
                },
                "read_only": True,             # Root filesystem is strictly read-only
                "detach": True
            }

            container = self.client.containers.create(**container_config)
            container.start()
            logger.info(f"Sandbox container started (ID: {container.short_id}). Running payload...")

            # 5. Monitor execution with strict active timeout
            container_start = time.time()
            exit_code = None
            
            while True:
                elapsed = time.time() - container_start
                if elapsed > timeout:
                    # Timeout boundary crossed, aggressively terminate
                    logger.warning(f"Payload execution exceeded timeout boundary of {timeout}s. Terminating container.")
                    try:
                        container.kill()
                    except Exception as e:
                        logger.error(f"Failed to kill timed out container: {e}")
                    container.remove(force=True)
                    return {
                        "status": "timeout",
                        "exit_code": -9,
                        "output": f"Execution Timed Out (exceeded limit of {timeout}s)",
                        "duration": round(time.time() - start_time, 3),
                        "error": "Container was forcefully terminated due to a timeout."
                    }

                # Check container status
                container.reload()
                state = container.attrs.get("State", {})
                if not state.get("Running", False):
                    exit_code = state.get("ExitCode", 0)
                    break
                time.sleep(0.1)

            # 6. Retrieve standard output and standard error logs
            logs = container.logs(stdout=True, stderr=True)
            output = logs.decode("utf-8", errors="ignore")

            # Trace output line-by-line at the INFO level
            logger.info("--- START SANDBOX OUTPUT ---")
            if output.strip():
                for line in output.splitlines():
                    logger.info(f"  [Sandbox Exec] {line}")
            else:
                logger.info("  [Sandbox Exec] (No console output returned)")
            logger.info("--- END SANDBOX OUTPUT ---")

            # Cleanup
            container.remove(force=True)
            logger.info("Sandbox container destroyed successfully.")

            return {
                "status": "success" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "output": output,
                "duration": round(time.time() - start_time, 3)
            }

        except ContainerError as ce:
            stderr_out = ce.stderr.decode("utf-8", errors="ignore")
            logger.error(f"Container runtime exception encountered. Exit status: {ce.exit_status}")
            for line in stderr_out.splitlines():
                logger.error(f"  [Sandbox StdErr] {line}")
            return {
                "status": "failed",
                "exit_code": ce.exit_status,
                "output": stderr_out,
                "duration": round(time.time() - start_time, 3),
                "error": "An error occurred during container execution."
            }
        except Exception as e:
            logger.error(f"Unhandled sandbox orchestrator exception: {str(e)}")
            return {
                "status": "error",
                "exit_code": -1,
                "output": f"System/Orchestration Error: {str(e)}",
                "duration": round(time.time() - start_time, 3),
                "error": f"An unhandled orchestrator exception occurred: {type(e).__name__}"
            }
        finally:
            # Clean up local temporary directories
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    logger.info("============================================================")
    logger.info("      AGENT SANDBOX INFRASTRUCTURE - DIAGNOSTICS & TEST      ")
    logger.info("============================================================")

    sandbox = AgentSandboxOrchestrator()
    ready, diagnostic_msg = sandbox.is_ready()

    if not ready:
        logger.error("DIAGNOSTIC CRITICAL FAILURE:")
        for line in diagnostic_msg.splitlines():
            logger.error(f"  [Diagnostic] {line}")
        logger.info("-" * 60)
        logger.info("💡 RECOVERY WORKFLOW STEPS:")
        logger.info("1. Verify Docker Desktop is active on this workstation.")
        logger.info("2. Confirm Python Docker dependency is installed: pip install docker")
        logger.info("3. Re-execute this script inside your active virtual environment.")
        logger.info("-" * 60)
        logger.info("============================================================")
        sys.exit(0)

    logger.info("Docker Client state validated. Executing safety test suite...")

    # TEST 1: Benign Execution Trace
    logger.info("--- TEST 1: Running Safe Python Code ---")
    safe_code = """
import sys
print("Hello from the isolated sandbox environment!")
print(f"Python interpreter: {sys.version}")
"""
    res1 = sandbox.run_payload(safe_code)
    logger.info(f"Test 1 Complete. Status: {res1['status']} | Exit: {res1['exit_code']} | Duration: {res1['duration']}s")
    logger.info("-" * 60)

    # TEST 2: Network Isolation Block
    logger.info("--- TEST 2: Testing Network Egress Isolation ---")
    socket_code = """
import socket
try:
    socket.create_connection(("8.8.8.8", 53), timeout=2.0)
    print("Security exploit warning: outbound network socket open!")
except Exception as e:
    print(f"Network egress blocked successfully: {e}")
"""
    res2 = sandbox.run_payload(socket_code)
    logger.info(f"Test 2 Complete. Status: {res2['status']} | Exit: {res2['exit_code']} | Duration: {res2['duration']}s")
    logger.info("-" * 60)

    # TEST 3: Timed Interruption
    logger.info("--- TEST 3: Testing Active Timeout Interruption ---")
    loop_code = """
import time
print("Starting long-running workload (infinite loop simulation)...")
while True:
    time.sleep(0.5)
"""
    res3 = sandbox.run_payload(loop_code, timeout=4.0)
    logger.info(f"Test 3 Complete. Status: {res3['status']} | Exit: {res3['exit_code']} | Duration: {res3['duration']}s")
    logger.info("============================================================")
