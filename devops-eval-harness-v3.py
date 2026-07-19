#!/usr/bin/env python3
"""
devops-eval-harness-v3.py
Anthropic Research Engineer (Universes) Portfolio - DevOps Evaluation Environment
This script provisions an isolated Docker container, injects a deterministic system-level bug,
exposes an execution command interface, and evaluates capability using a rigorous reward verifier.
Features complete python logging at the INFO level with line-by-line tracing of all outputs.
"""

import sys
import time
import json
import base64
import logging
import traceback
from typing import Dict, Any

# Configure structured python logging at the INFO level
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("DevOpsEvalHarness")

# Verify Docker library dependency
try:
    import docker
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False
    logger.error("The 'docker' Python package is not installed.")


# Python microservice code that crashes with invalid non-int ports
MICROSERVICE_CODE = """
import os
import sys
import http.server
import socketserver

def main():
    config_path = "/app/config.env"
    if not os.path.exists(config_path):
        print(f"Error: Config file {config_path} not found", file=sys.stderr)
        sys.exit(1)
        
    port = None
    with open(config_path, "r") as f:
        for line in f:
            if line.strip().startswith("PORT="):
                val = line.strip().split("=", 1)[1].strip("'\\\"")
                try:
                    port = int(val)
                except ValueError:
                    print(f"CRITICAL: Invalid port configuration '{val}'. Port must be an integer.", file=sys.stderr)
                    sys.exit(1)
                    
    if port is None:
        print("CRITICAL: PORT variable not defined in config.env", file=sys.stderr)
        sys.exit(1)
        
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK - Service Healthy")
            
        def log_message(self, format, *args):
            # Suppress default server logs on stdout to keep evaluation clean
            pass
            
    print(f"Starting server on port {port}...")
    sys.stdout.flush()
    socketserver.TCPServer.allow_reuse_address = True
    try:
        with socketserver.TCPServer(("", port), Handler) as httpd:
            httpd.serve_forever()
    except Exception as e:
        print(f"CRITICAL: Failed to start server: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
"""


class DevOpsEvalHarness:
    def __init__(self, image_name="python:3.11-alpine"):
        self.image_name = image_name
        self.client = None
        self.container = None
        self.trajectory = []
        self.start_time = None

    def check_docker(self) -> bool:
        """Verifies connection to local Docker daemon."""
        if not DOCKER_AVAILABLE:
            logger.error("The 'docker' Python package is missing. Cannot proceed.")
            return False
        try:
            self.client = docker.from_env()
            self.client.ping()
            return True
        except Exception as e:
            logger.error(f"Docker Connection Failed. Ensure Docker Desktop is active on this host. Error: {e}")
            return False

    def setup_environment(self):
        """Provisions alpine container and sets up broken config & microservice."""
        logger.info(f"Staging environment: pulling baseline image '{self.image_name}' if missing...")
        try:
            self.client.images.pull(self.image_name)
        except Exception:
            logger.warning("Image pull failed or timed out. Attempting execution with cached local images...")

        logger.info("Spawning isolated evaluation container...")
        self.container = self.client.containers.run(
            self.image_name,
            command="sleep 3600",
            detach=True,
            network_mode="bridge",  # Bridge network for health probing
            mem_limit="256m",
            nano_cpus=1000000000,    # Hard limit to 1 CPU core max
            name=f"devops-eval-{int(time.time())}"
        )

        logger.info("Initializing workspace and writing files securely via base64 pipeline...")
        # Create application workspace directory inside container
        self.container.exec_run("mkdir -p /app")

        # Encode and write python microservice
        encoded_service = base64.b64encode(MICROSERVICE_CODE.encode('utf-8')).decode('utf-8')
        write_service_cmd = f"sh -c 'echo {encoded_service} | base64 -d > /app/server.py'"
        res_service = self.container.exec_run(write_service_cmd)
        if res_service.exit_code != 0:
            raise RuntimeError(f"Failed to inject microservice source: {res_service.output.decode('utf-8')}")

        # Encode and write broken configuration file
        config_content = "PORT=8080-crash\n"
        encoded_config = base64.b64encode(config_content.encode('utf-8')).decode('utf-8')
        write_config_cmd = f"sh -c 'echo {encoded_config} | base64 -d > /app/config.env'"
        res_config = self.container.exec_run(write_config_cmd)
        if res_config.exit_code != 0:
            raise RuntimeError(f"Failed to write broken configuration profile: {res_config.output.decode('utf-8')}")

        logger.info("Sandbox workspace initialized successfully with broken system configuration (PORT=8080-crash).")

    def execute_agent_command(self, command: str) -> dict:
        """Executes a command inside the container as the simulated AI Agent."""
        if not self.container:
            raise RuntimeError("Container sandbox is not active.")
        
        start = time.time()
        logger.info(f"[Agent Execution] command -> {command}")

        # Execute command inside container
        res = self.container.exec_run(command)
        duration = round(time.time() - start, 4)
        stdout = res.output.decode('utf-8', errors='ignore')

        # Log each line of command stdout at the INFO level
        if stdout.strip():
            logger.info("--- START AGENT STDOUT ---")
            for line in stdout.splitlines():
                logger.info(f"  [Stdout] {line}")
            logger.info("--- END AGENT STDOUT ---")
        else:
            logger.info("  [Stdout] (No command-line output)")

        action_log = {
            "command": command,
            "exit_code": res.exit_code,
            "stdout": stdout,
            "duration_sec": duration,
            "timestamp": round(time.time() - self.start_time, 2) if self.start_time else 0
        }
        self.trajectory.append(action_log)
        return action_log

    def run_verifier(self) -> dict:
        """
        The Reward Model / Verifier.
        Checks the configuration variables, process listing, and tests network accessibility.
        """
        metrics = {
            "config_valid": False,
            "process_active": False,
            "endpoint_healthy": False,
            "score": 0.0,
            "details": ""
        }

        # 1. Config Check
        res_config = self.container.exec_run("cat /app/config.env")
        if res_config.exit_code == 0:
            config_text = res_config.output.decode('utf-8')
            for line in config_text.splitlines():
                if "PORT=" in line:
                    port_val = line.split("=")[1].strip("'\" ")
                    try:
                        port = int(port_val)
                        metrics["config_valid"] = True
                        break
                    except ValueError:
                        metrics["details"] += "[Verifier Fail] config.env specifies a non-integer PORT. "
            if not metrics["config_valid"] and not metrics["details"]:
                metrics["details"] += "[Verifier Fail] config.env contains no valid PORT mapping. "
        else:
            metrics["details"] += "[Verifier Fail] config.env file does not exist. "

        # 2. Process Check
        res_ps = self.container.exec_run("pgrep -f server.py")
        if res_ps.exit_code == 0 and res_ps.output.strip():
            metrics["process_active"] = True
        else:
            metrics["details"] += "[Verifier Fail] Python process 'server.py' is not running in background. "

        # 3. Endpoint Connection Check
        if metrics["config_valid"] and metrics["process_active"]:
            test_cmd = f"sh -c 'wget -q -O- http://127.0.0.1:{port}'"
            res_test = self.container.exec_run(test_cmd)
            test_output = res_test.output.decode('utf-8', errors='ignore')
            if res_test.exit_code == 0 and "Service Healthy" in test_output:
                metrics["endpoint_healthy"] = True
            else:
                metrics["details"] += f"[Verifier Fail] Probe connection to port {port} failed or returned bad response. "

        # Compute final reward weights
        score = 0.0
        if metrics["config_valid"]:
            score += 0.3
        if metrics["process_active"]:
            score += 0.3
        if metrics["endpoint_healthy"]:
            score += 0.4
        
        metrics["score"] = round(score, 1)
        if metrics["score"] == 1.0:
            metrics["details"] = "All verification constraints met. Application is healthy!"
        
        return metrics

    def run_simulation(self) -> dict:
        """Simulates an AI agent repairing the server environment step-by-step."""
        logger.info("============================================================")
        logger.info("          STARTING AI AGENT EVALUATION SIMULATION           ")
        logger.info("============================================================")
        
        self.start_time = time.time()
        self.trajectory = []

        # Step 1: Baseline Launch Attempt (Demonstrating crash)
        logger.info("[AGENT STEP 1] Attempting execution of python microservice...")
        step1 = self.execute_agent_command("python3 /app/server.py")
        
        # Verify initial reward model state
        v_init = self.run_verifier()
        logger.info(f"Baseline Verification Result - Score: {v_init['score']}/1.0 | Status: {v_init['details']}")

        # Step 2: Inspection of environment
        logger.info("[AGENT STEP 2] Inspecting application settings profile...")
        self.execute_agent_command("cat /app/config.env")

        # Step 3: Repairing config error inside container
        logger.info("[AGENT STEP 3] Executing in-place sed regex substitution to fix the port configuration...")
        self.execute_agent_command("sh -c \"sed -i 's/8080-crash/8080/g' /app/config.env\"")
        
        # Verify fix was applied successfully
        self.execute_agent_command("cat /app/config.env")

        # Step 4: Background Service Execution
        logger.info("[AGENT STEP 4] Launching python microservice in background mode and piping outputs...")
        self.execute_agent_command("sh -c 'python3 /app/server.py > /app/server.log 2>&1 &'")
        
        logger.info("Allowing background service boot buffer of 2.0s...")
        time.sleep(2.0)

        # Step 5: Read server startup logs to double-check state
        logger.info("[AGENT STEP 5] Inspecting stdout/stderr server startup streams...")
        self.execute_agent_command("cat /app/server.log")

        # Step 6: Trigger final independent verifier run
        logger.info("============================================================")
        logger.info("            TRIGGERING SYSTEM VERIFICATION RUN              ")
        logger.info("============================================================")
        
        v_final = self.run_verifier()
        logger.info(f"Final Verification Score: {v_final['score']}/1.0")
        logger.info(f"Status Details: {v_final['details']}")

        # Assemble final structured trajectory log
        eval_result = {
            "eval_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "overall_status": "SUCCESS" if v_final["score"] == 1.0 else "FAILED",
            "final_score": v_final["score"],
            "agent_actions_taken": len(self.trajectory),
            "trajectory": self.trajectory,
            "verifier_metrics": v_final
        }

        # Save trajectory JSON to container disk for post-training / inspection auditing
        self.container.exec_run(f"sh -c \"echo '{json.dumps(eval_result, indent=2)}' > /app/trajectory.json\"")
        logger.info("Evaluation finalized. Trajectory transcript saved to sandbox: /app/trajectory.json")
        
        return eval_result

    def cleanup(self):
        """Teardown the active evaluation container."""
        if self.container:
            logger.info("Terminating and destroying sandbox container...")
            try:
                self.container.stop(timeout=1)
                self.container.remove()
                logger.info("Sandbox container destroyed. Environment cleaned.")
            except Exception as e:
                logger.error(f"Error encountered during environment cleanup: {e}")


if __name__ == "__main__":
    logger.info("============================================================")
    logger.info("      ANTHROPIC DEVOPS EVALUATION ENVIRONMENT - SIMULATOR    ")
    logger.info("============================================================")

    harness = DevOpsEvalHarness()
    if not harness.check_docker():
        logger.error("Pre-flight Docker connection failure. Simulation aborted.")
        sys.exit(1)

    try:
        harness.setup_environment()
        eval_data = harness.run_simulation()
        
        # Log summary of results to console
        logger.info("==================== EVALUATION SUMMARY ====================")
        logger.info(json.dumps(eval_data["verifier_metrics"], indent=2))
        logger.info("============================================================")
        
    except Exception as e:
        logger.error(f"Failed during evaluation lifecycle: {e}")
        traceback.print_exc()
    finally:
        harness.cleanup()
