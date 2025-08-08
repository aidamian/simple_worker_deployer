import os
import docker
import requests
import threading
import time
import signal
from urllib.parse import urlparse

class ContainerManager:
  """
  Manages a Docker container running a Node.js application from a GitHub repo.
  Handles starting the container, streaming logs, polling an endpoint, checking for repo updates,
  and cleaning up the container on exit or when a new commit is detected.
  """
  def __init__(
    self, 
    repo_url, 
    build_and_run_commands, 
    image="node:18", 
    poll_interval=10,
    env={},
):
    self.repo_url = repo_url
    self.build_and_run_commands = build_and_run_commands
    self.image = image
    self.poll_interval = poll_interval
    self.docker_client = docker.from_env()
    # Parse repo information for commit checking
    parsed = urlparse(repo_url)
    self.git_token = parsed.password  # PAT for GitHub
    # Extract owner and repo name from URL path (strip leading '/' and .git suffix)
    path = parsed.path.lstrip("/")
    if path.endswith(".git"):
      path = path[:-4]
    owner_repo = path  # e.g. "owner/repo"
    if "/" in owner_repo:
      self.repo_owner, self.repo_name = owner_repo.split("/", 1)
    else:
      # In case the URL path is not in expected format
      self.repo_owner = owner_repo
      self.repo_name = ""
    # Determine default branch via GitHub API (so we know which branch to monitor)
    self.branch = None
    if self.repo_owner and self.repo_name:
      try:
        api_url = f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}"
        headers = {"Authorization": f"token {self.git_token}"} if self.git_token else {}
        resp = requests.get(api_url, headers=headers, timeout=5)
        if resp.status_code == 200:
          data = resp.json()
          self.branch = data.get("default_branch", None)
      except Exception as e:
        self.P(f"[WARN] Could not determine default branch: {e}")
    if not self.branch:
      self.branch = "main"  # Fallback to 'main' if not determined

    # Internal state
    self.container = None
    self.log_thread = None
    self._stop_event = threading.Event()

    # Ensure container is cleaned up on program exit (signal handling)
    signal.signal(signal.SIGINT, self._handle_signal)
    signal.signal(signal.SIGTERM, self._handle_signal)
    
  def P(self, s, **kwargs):
    """Prints debug information with a prefix."""
    kwargs['flush'] = True
    print("[DEBUG] " + s, **kwargs)
    return

  def _handle_signal(self, signum, frame):
    """Signal handler for graceful termination."""
    self.P("\n[INFO] Termination signal received (signal {}). Stopping container...".format(signum))
    self.stop_container()  # Stop and remove container if running
    # After stopping, set the event to signal threads to exit if needed
    self._stop_event.set()

  def start_container(self):
    """Start the Docker container and run the git clone + build commands inside it."""
    # Build the shell command to run inside container
    # Clone the repo into /app and then run the build and start commands in /app
    shell_cmd = (
      f"git clone {self.repo_url} /app && cd /app && " +
      " && ".join(self.build_and_run_commands)
    )
    self.P(f"[INFO] Launching container with image '{self.image}'...")
    # Run the container in detached mode, with port 3000 published to host
    # Using sh -c to run the shell command
    self.P("Running command in container: {}".format(shell_cmd))
    self.container = self.docker_client.containers.run(
      self.image, command=["sh", "-c", shell_cmd],
      detach=True, ports={"3000/tcp": 3000}, 
      environment=self.env
    )
    self.P(f"[INFO] Container started (ID: {self.container.short_id}). Cloning repo and starting app...")

    # Start a background thread to stream container logs
    self.log_thread = threading.Thread(target=self._stream_logs, daemon=True)
    self.log_thread.start()

  def _stream_logs(self):
    """Stream the container's stdout/stderr to the host stdout in real-time."""
    if not self.container:
      return
    # Attach to logs (stdout+stderr) and stream line by line
    try:
      for log_bytes in self.container.logs(stream=True, stdout=True, stderr=True, follow=True):
        if log_bytes is None:
          break
        # Decode bytes to string
        try:
          log_str = log_bytes.decode("utf-8", errors="replace")
        except Exception:
          log_str = str(log_bytes)
        # Print each log line with a prefix to distinguish container output
        self.P(f"[CONTAINER] {log_str}", end="")
        # If stop event is set, break out to stop streaming
        if self._stop_event.is_set():
          break
    except Exception as e:
      self.P(f"[ERROR] Exception while streaming logs: {e}")

  def poll_endpoint(self):
    """Poll the container's /edgenode endpoint and print the response."""
    url = "http://localhost:3000/edgenode"
    try:
      resp = requests.get(url, timeout=5)
      status = resp.status_code
      # Truncate response text if it's very long
      text = resp.text
      if len(text) > 200:
        text = text[:200] + "..." 
      self.P(f"[INFO] GET {url} -> {status}, Response: {text}")
    except requests.RequestException as e:
      self.P(f"[INFO] GET {url} failed: {e}")

  def get_latest_commit(self):
    """Fetch the latest commit SHA of the repository's monitored branch via GitHub API."""
    if not self.repo_owner or not self.repo_name or not self.branch:
      return None
    api_url = f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}/branches/{self.branch}"
    headers = {"Authorization": f"token {self.git_token}"} if self.git_token else {}
    try:
      resp = requests.get(api_url, headers=headers, timeout=5)
      data = resp.json() if resp.status_code == 200 else {}
      latest_sha = data.get("commit", {}).get("sha", None)
      return latest_sha
    except Exception as e:
      self.P(f"[WARN] Failed to fetch latest commit: {e}")
      return None

  def stop_container(self):
    """Stop and remove the Docker container if it is running."""
    if self.container:
      try:
        # Stop the container (gracefully)
        self.container.stop(timeout=5)
      except Exception as e:
        self.P(f"[WARN] Error stopping container: {e}")
      try:
        self.container.remove()
      except Exception as e:
        self.P(f"[WARN] Error removing container: {e}")
      finally:
        self.container = None

  def run(self):
    """Run the container and monitor it, restarting on new commits and handling graceful shutdown."""
    self.P("[INFO] Starting container manager...")
    current_commit = self.get_latest_commit()
    if current_commit:
      self.P(f"[INFO] Latest commit on {self.branch}: {current_commit}")

    try:
      # Initial container launch
      self.start_container()
      running = True
      while running:
        # Sleep for the poll interval
        time.sleep(self.poll_interval)
        # Poll the application endpoint
        self.poll_endpoint()
        # Check for new commits in the repository
        latest_commit = self.get_latest_commit()
        if latest_commit and current_commit and latest_commit != current_commit:
          self.P(f"[INFO] New commit detected ({latest_commit[:7]} != {current_commit[:7]}). Restarting container...")
          # Update current_commit to the new one
          current_commit = latest_commit
          # Stop and remove current container, and end its log thread
          self.stop_container()
          self._stop_event.set()  # signal log thread to stop if running
          if self.log_thread:
            self.log_thread.join(timeout=5)
          # Start a new container with the updated code
          self._stop_event.clear()  # reset stop flag for new log thread
          self.start_container()
          continue  # continue monitoring with new container
        # If container has stopped on its own (unexpectedly), break out to end the loop
        if self.container:
          # Refresh container status
          self.container.reload()
          if self.container.status != "running":
            self.P("[ERROR] Container stopped unexpectedly (exit code {}).".format(
                  self.container.attrs.get("State", {}).get("ExitCode")))
            running = False
    except KeyboardInterrupt:
      # Handle Ctrl+C gracefully (SIGINT handled by signal handler too)
      self.P("\n[INFO] KeyboardInterrupt received. Shutting down...")
      # (The signal handler will also invoke stop_container)
    finally:
      # Ensure container is cleaned up if still running
      self.stop_container()
      # Ensure log thread is stopped
      self._stop_event.set()
      if self.log_thread:
        self.log_thread.join(timeout=5)
      self.P("[INFO] Container manager has exited.")

# Usage example (replace with actual repo URL with credentials):
if __name__ == "__main__":
  username = os.environ.get("WORKER_GIT_USERNAME")
  pat = os.environ.get("WORKER_GIT_PAT")
  # https://github.com/aidamian/private-worker-app.git
  repo = f"https://{username}:{pat}@github.com/aidamian/private-worker-app.git"
  commands = ["npm install", "npm start"]
  manager = ContainerManager(
    repo_url=repo, 
    build_and_run_commands=commands, 
    image="node:18", 
    poll_interval=10,
    env={"EE_HOST_ID": "test1", "EE_HOST_ADDR": "0xai", "EE_HOST_ETH_ADDR": "0xBEEF"}    
  )
  manager.run()
