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
    self.env = env  # Environment variables to pass to the container
    self.done = False  # Flag to indicate when to stop the main loop
    self.current_commit = None  # Track the current commit SHA
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
    return
  
    
  def P(self, s, color='w', **kwargs):
    """Prints in color (white, dark, blue, red, green, yellow) debug information with a prefix."""
    COLORS = {
      'w': '\033[0m',  # white
      'd': '\033[90m',  # dark gray
      'b': '\033[94m',  # blue
      'r': '\033[91m',  # red
      'g': '\033[92m',  # green
      'y': '\033[93m',  # yellow
    }
    if color not in COLORS:
      color = 'w'
    s = '[INFO] ' + str(s)  # Ensure s is a string
    s = f"{COLORS[color]}{s}\033[0m"  #
    kwargs['flush'] = True
    print(s, **kwargs)
    return

  def _handle_signal(self, signum, frame):
    """Signal handler for graceful termination."""
    self.P("\nTermination signal received (signal {}). Stopping container...".format(signum))
    self.stop_container()  # Stop and remove container if running
    # After stopping, set the event to signal threads to exit if needed
    self._stop_event.set()
    self.done = True  # Set done flag to exit main loop
    self.P("Exiting gracefully...")
    return


  def start_container(self):
    """Start the Docker container without running build or app commands."""
    self.P(f"Launching container with image '{self.image}'...")
    # Run the base container in detached mode with a long running sleep so it stays alive
    self.container = self.docker_client.containers.run(
      self.image,
      command=["sh", "-c", "while true; do sleep 3600; done"],
      detach=True,
      ports={"3000/tcp": 3000},
      environment=self.env,
    )
    self.P(f"Container started (ID: {self.container.short_id}).")
    return self.container


  def execute_build_and_run_cmds(self):
    """Clone the repository and execute build/run commands inside the running container."""
    if not self.container:
      raise RuntimeError("Container must be started before executing commands")

    shell_cmd = (
      f"git clone {self.repo_url} /app && cd /app && " +
      " && ".join(self.build_and_run_commands)
    )
    self.P("Running command in container: {}".format(shell_cmd))
    # Execute the command and obtain a streaming iterator without blocking
    exec_result = self.container.exec_run(["sh", "-c", shell_cmd], stream=True, detach=False)
    # Consume the iterator in a background thread so the main thread stays free
    self.log_thread = threading.Thread(
      target=self._stream_logs,
      args=(exec_result.output,),
      daemon=True,
    )
    self.log_thread.start()
    return


  def _get_container_memory(self):
    """Return current memory usage of the container in bytes."""
    if not self.container:
      return 0
    try:
      stats = self.container.stats(stream=False)
      return stats.get("memory_stats", {}).get("usage", 0)
    except Exception as e:
      self.P(f"[WARN] Could not fetch memory usage: {e}")
      return 0


  def launch_container_app(self):
    """Start container, then build and run the app, recording memory usage before and after."""
    container = self.start_container()
    # Memory usage before installing the app
    mem_before_mb = self._get_container_memory() / (1024 ** 2)

    # Execute build and run commands
    self.execute_build_and_run_cmds()

    # Allow some time for the app to start before measuring again
    time.sleep(1)
    mem_after_mb = self._get_container_memory() / (1024 ** 2)
    self.P(f"Container memory usage before build/run: {mem_before_mb:>5.0f} MB")
    self.P(f"Container memory usage after build/run:  {mem_after_mb:>5.0f} MB")

    return container


  def restart_from_scratch(self):
    """Stop the current container and start a new one from scratch."""
    self.P("Restarting container from scratch...")
    self.stop_container()
    self._stop_event.set()  # signal log thread to stop if running
    if self.log_thread:
      self.log_thread.join(timeout=5)
    # Start a new container with the updated code
    self._stop_event.clear()  # reset stop flag for new log thread
    return self.launch_container_app()


  def _stream_logs(self, log_stream):
    """Consume a log iterator from exec_run and print its output."""
    if not log_stream:
      return
    try:
      for log_bytes in log_stream:
        if log_bytes is None:
          break
        try:
          log_str = log_bytes.decode("utf-8", errors="replace")
        except Exception:
          log_str = str(log_bytes)
        self.P(f"[CONTAINER] {log_str}", color='d', end='')
        if self._stop_event.is_set():
          break
    except Exception as e:
      self.P(f"[ERROR] Exception while streaming logs: {e}")
    return


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
      self.P(f"GET {url} -> {status}, Response: {text}")
    except requests.RequestException as e:
      self.P(f"GET {url} failed: {e}")
    return
  

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
    return


  def run(self):
    """Run the container and monitor it, restarting on new commits and handling graceful shutdown."""
    self.P("Starting container manager...")
    current_commit = self.get_latest_commit()
    if current_commit:
      self.P(f"Latest commit on {self.branch}: {current_commit}")

    try:
      # Initial container launch
      self.launch_container_app()
      self.done = False
      while not self.done:
        # Sleep for the poll interval
        time.sleep(self.poll_interval)
        # Poll the application endpoint
        self.poll_endpoint()
        # Check for new commits in the repository
        latest_commit = self.get_latest_commit()
        if latest_commit and self.current_commit and latest_commit != self.current_commit:
          self.P(f"New commit detected ({latest_commit[:7]} != {self.current_commit[:7]}). Restarting container...")
          # Update current_commit to the new one
          self.current_commit = latest_commit
          # Stop and remove current container, and end its log thread
          self.restart_from_scratch()
          continue  # continue monitoring with new container
        # If container has stopped on its own (unexpectedly), break out to end the loop
        if self.container:
          # Refresh container status
          self.container.reload()
          if self.container.status != "running":
            self.P("[ERROR] Container stopped unexpectedly (exit code {}).".format(
                  self.container.attrs.get("State", {}).get("ExitCode")))
            self.done = True
    except KeyboardInterrupt:
      # Handle Ctrl+C gracefully (SIGINT handled by signal handler too)
      self.P("\nKeyboardInterrupt received. Shutting down...")
      # (The signal handler will also invoke stop_container)
    finally:
      # Ensure container is cleaned up if still running
      self.stop_container()
      # Ensure log thread is stopped
      self._stop_event.set()
      if self.log_thread:
        self.log_thread.join(timeout=5)
      self.P("Container manager has exited.")
    return


# Usage example (replace with actual repo URL with credentials):
if __name__ == "__main__":
  username = os.environ.get("WORKER_GIT_USERNAME")
  pat = os.environ.get("WORKER_GIT_PAT")
  if len(pat) < 10:
    print("[ERROR] PAT is too short!")
  else:
    print(f"Using GitHub credentials: {username} (PAT length: {len(pat) if pat else 0})") 
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
