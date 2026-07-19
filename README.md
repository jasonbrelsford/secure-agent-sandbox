# Agent Sandbox & DevOps Evaluation Suite

An enterprise-grade orchestration and benchmarking suite designed to securely execute untrusted LLM-generated code and rigorously evaluate AI agent capabilities in containerized systems. 

This repository directly addresses the engineering bottlenecks faced by frontier AI safety labs (specifically targeting the core missions of teams like **Anthropic's Universes team**): building secure, hardened VM/container sandboxes, establishing objective verifiers (reward models) to measure model capabilities, and generating multi-turn trajectories [68, 69, 70, 72].

---

## System Architecture

The suite is divided into two primary modules:
1. **The Hardened Execution Sandbox (`orchestrator.py`)**: A defense-in-depth orchestration layer utilizing the Docker SDK to run arbitrary code under strict, hyper-hardened security constraints.
2. **The DevOps Evaluation Harness (`devops_eval.py`)**: A simulated agent workspace that provisions a broken environment, exposes a secure command interface acting as the agent's bash shell, runs an independent verifier, and compiles structured JSON trajectories.

```
                  +----------------------------------------------+
                  |         Host Machine (Docker Daemon)         |
                  +----------------------+-----------------------+
                                         |
                       Spawns Hardened Alpine Sandbox
                                         v
                  +----------------------------------------------+
                  |        Isolated Container Workspace          |
                  |                                              |
                  |  [NETWORK]     network_mode = "none"         |  <-- Block internet egress
                  |  [CAPS]        cap_drop = ["ALL"]            |  <-- Remove kernel capabilities
                  |  [PRIVILEGES]  no-new-privileges:true        |  <-- Prevent escalation/sudo
                  |  [USER]        user = "1000:1000"            |  <-- Unprivileged non-root runner
                  |                                              |
                  |  [MOUNT 1]     /app/payload.py [Read-Only]   |  <-- Safe code execution mount
                  |  [MOUNT 2]     /tmp (Transient) [Read-Write]  |  <-- Temporary workspace
                  |                                              |
                  |  [LIMITS]      mem_limit = "128m"            |  <-- Prevent memory-exhaustion (DoS)
                  |                cpu_quota = 50000 (0.5 CPU)   |  <-- Prevent CPU-exhaustion (DoS)
                  +----------------------+-----------------------+
                                         |
                               Host Timeout Watcher
                                         v
                             [Kills container if > 5.0s]
```

---

## Module 1: Hardened Code Sandbox (`orchestrator.py`)

To execute raw, untrusted Python payloads safely, the orchestrator implements a **defense-in-depth model** that locks down containers at the kernel level:

   **Total Network Blackout**: Containers are configured with `network_mode="none"`. This strictly blocks socket-level connections, preventing LLMs from exfiltrating system data, executing remote access exploits, or scanning local home networks.
   **Kernel-Level Capability Stripping**: Drops all system capabilities via `cap_drop=["ALL"]`. This blocks lower-level system operations, making privilege escalation practically impossible.
   **Unprivileged Non-Root Execution**: Runs under the UID `1000:1000` instead of the standard container `root`.
   **Strict Read-Only Filesystem**: The container root filesystem is entirely read-only. A transient, isolated `/tmp` workspace is provided as a writable directory for standard input/output generation, ensuring no permanent modifications can be made to the base system.
   **Aggressive Host-Side Timeout Watcher**: If the agent's code enters an infinite loop, a dedicated background loop on the host machine counts down (default: `5.0s`) and fires an aggressive `SIGKILL` directly through the Docker daemon, ensuring host system stability.

---

## Module 2: DevOps Evaluation Harness (`devops_eval.py`)

A primary duty of evaluation research engineers is creating environments that accurately measure model capabilities. This harness implements a robust, multi-stage evaluation loop that simulates a real production engineering challenge:

```
+------------------------+      +------------------------+      +------------------------+
| 1. Initialize Task     | ---> | 2. Agent Execution     | ---> | 3. Verifier Assessment |
| Configures container   |      | Exposes monitored,     |      | Runs multi-point independent |
| with a broken port     |      | timed terminal access  |      | health checks to grade |
| variable (non-int)     |      | for the agent's shell  |      | the final system state |
+------------------------+      +------------------------+      +------------------------+
                                                                             |
                                                                             v
                                                                +------------------------+
                                                                | 4. Compile Trajectory  |
                                                                | Generates a structured |
                                                                | JSON audit containing   |
                                                                | commands, logs, & score|
+------------------------+                                      +------------------------+
```

1.  **Broken State Initialization**: Instantiates an Alpine Linux container and deploys a Python-based HTTP microservice alongside an intentional, non-integer system configuration: `PORT=8080-crash`.
2.  **Monitored Command Channel**: Exposes an execution method acting as the agent’s bash terminal. It limits command run-times, captures stdout/stderr, and returns output to the model.
3.  **The Independent Verifier (Reward Model)**: Independently assesses system state on three metrics:
    *   **Configuration Validation**: Confirms `config.env` exists and contains a valid integer port.
    *   **Runtime Verification**: Scans processes (`pgrep`) to ensure the server is active.
    *   **E2E Endpoint Probing**: Fires local loopback requests (`wget`) to confirm the service is responding with a healthy `200 OK`.
4.  **SFT Trajectory Compilation**: Captures all agent actions, system states, durations, and verifier results into a structured JSON transcript, facilitating supervised fine-tuning (SFT) dataset curation.

---

## 🔬 Case Study: The Escape-Proof Base64 Deployment Pipeline

### The Problem: Shell Redirection Shell-Escapes
During early development, the orchestration layer pushed Python and configuration files into containers using standard shell redirects over the Docker SDK:
```bash
# Naive approach:
docker.containers.run("echo 'payload_string' > /app/server.py")
```
This naive method failed immediately when scaling up the container's microservice code. Because raw source code contains a mix of single quotes (`'`), double quotes (`"`), indentation line-breaks, and format parameters, **the container's shell interpreter split the command strings incorrectly**. This triggered a critical `No closing quotation` syntax crash before execution could even begin.

### The Solution: Quoting-Immune Base64 Pipes
To make file deployment 100% deterministic and immune to shell-escaping exploits, the suite implements a **Base64 Deployment Pipeline**:

```python
# 1. Host side: Safe encoding into a flat, alphanumeric ASCII string
encoded_service = base64.b64encode(MICROSERVICE_CODE.encode('utf-8')).decode('utf-8')

# 2. Container side: Write command receives safe ASCII and decodes on-the-fly
write_service_cmd = f"sh -c 'echo {encoded_service} | base64 -d > /app/server.py'"
res_service = self.container.exec_run(write_service_cmd)
```

By encoding the payloads into Base64, we strip away all problematic quotes, semicolons, and special shell operators on the host. The container receives a flat, secure string, pipes it to the native command-line decoder `base64 -d`, and reconstructs the target file flawlessly on disk.

---

## Observability Layer

Both orchestrators utilize Python’s structured `logging` library configured at the `INFO` level. Instead of printing massive, unformatted text blocks to console, the system splits stdout/stderr streams and logs **each individual terminal line** with localized timestamps:

```text
2026-07-19 14:15:02,125 [INFO] DevOpsEvalHarness - [Agent Action] cat /app/config.env
2026-07-19 14:15:02,128 [INFO] DevOpsEvalHarness - --- START AGENT STDOUT ---
2026-07-19 14:15:02,129 [INFO] DevOpsEvalHarness -   PORT=8080-crash
2026-07-19 14:15:02,129 [INFO] DevOpsEvalHarness - --- END AGENT STDOUT ---
2026-07-19 14:15:02,130 [INFO] DevOpsEvalHarness - [Verification] Running independent verifier / scoring model...
2026-07-19 14:15:02,135 [INFO] DevOpsEvalHarness - --> Evaluation Score: 0.0/1.0
```

---

## Quick Start

### Prerequisites
*   [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running.
*   Python 3.8+ virtual environment.

### Setup & Run Test Suite
Clone the repo, set up your environment, and execute the orchestrator tests (safe execution, network block check, and active timeout interruption):

```bash
# 1. Install the Docker Python SDK
pip install docker

# 2. Run the secure sandbox orchestrator diagnostics
python agent-sandbox-v3.py

# 3. Run the DevOps evaluation simulation
python devops-eval-harness-v3.py
```
