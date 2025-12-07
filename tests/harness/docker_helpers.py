"""Docker build and run utilities."""

import asyncio
import json
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from tests.harness.models import (
    BuildResult,
    HealthCheckConfig,
    HealthCheckType,
    RuntimeConfig,
    RuntimeResult,
)


class DockerClient:
    """Async Docker client wrapper using CLI."""

    def __init__(self, timeout: int = 300):
        """Initialize Docker client.

        Args:
            timeout: Default command timeout in seconds
        """
        self.timeout = timeout

    async def _run_command(
        self,
        args: list[str],
        timeout: int | None = None,
    ) -> tuple[int, str, str]:
        """Run a docker command.

        Args:
            args: Command arguments
            timeout: Command timeout

        Returns:
            Tuple of (returncode, stdout, stderr)
        """
        timeout = timeout or self.timeout

        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
            return proc.returncode or 0, stdout.decode(), stderr.decode()
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "", f"Command timed out after {timeout}s"

    async def build(
        self,
        context_path: Path,
        dockerfile: str = "Dockerfile",
        tag: str | None = None,
        build_args: dict[str, str] | None = None,
        target: str | None = None,
        no_cache: bool = False,
    ) -> BuildResult:
        """Build a Docker image.

        Args:
            context_path: Path to build context
            dockerfile: Dockerfile name/path relative to context
            tag: Image tag
            build_args: Build arguments
            target: Target stage for multi-stage builds
            no_cache: Disable build cache

        Returns:
            BuildResult with build information
        """
        start_time = time.time()

        tag = tag or f"dfc-test-{uuid.uuid4().hex[:8]}"

        args = ["build", "-t", tag, "-f", str(context_path / dockerfile)]

        if build_args:
            for k, v in build_args.items():
                args.extend(["--build-arg", f"{k}={v}"])

        if target:
            args.extend(["--target", target])

        if no_cache:
            args.append("--no-cache")

        args.append(str(context_path))

        returncode, stdout, stderr = await self._run_command(args, timeout=600)

        build_time = time.time() - start_time

        if returncode != 0:
            return BuildResult(
                success=False,
                image_tag=tag,
                build_time_seconds=build_time,
                logs=stdout + stderr,
                errors=[stderr] if stderr else ["Build failed"],
            )

        # Get image ID
        _, image_id, _ = await self._run_command(
            ["images", "-q", tag],
            timeout=10,
        )

        return BuildResult(
            success=True,
            image_id=image_id.strip(),
            image_tag=tag,
            build_time_seconds=build_time,
            logs=stdout,
        )

    async def run(
        self,
        image: str,
        command: list[str] | None = None,
        environment: dict[str, str] | None = None,
        ports: dict[int, int] | None = None,
        detach: bool = True,
        remove: bool = False,
        name: str | None = None,
    ) -> tuple[str, str]:
        """Run a container.

        Args:
            image: Image to run
            command: Command to execute
            environment: Environment variables
            ports: Port mappings (container -> host, 0 = dynamic)
            detach: Run in background
            remove: Remove container when stopped
            name: Container name

        Returns:
            Tuple of (container_id, error_message)
        """
        name = name or f"dfc-test-{uuid.uuid4().hex[:8]}"

        args = ["run"]

        if detach:
            args.append("-d")

        if remove:
            args.append("--rm")

        args.extend(["--name", name])

        if environment:
            for k, v in environment.items():
                args.extend(["-e", f"{k}={v}"])

        if ports:
            for container_port, host_port in ports.items():
                if host_port == 0:
                    args.extend(["-p", f"{container_port}"])
                else:
                    args.extend(["-p", f"{host_port}:{container_port}"])

        args.append(image)

        if command:
            args.extend(command)

        returncode, stdout, stderr = await self._run_command(args, timeout=60)

        if returncode != 0:
            return "", stderr

        container_id = stdout.strip()
        return container_id, ""

    async def stop(self, container_id: str, timeout: int = 10) -> bool:
        """Stop a container.

        Args:
            container_id: Container to stop
            timeout: Stop timeout

        Returns:
            True if successful
        """
        returncode, _, _ = await self._run_command(
            ["stop", "-t", str(timeout), container_id],
            timeout=timeout + 5,
        )
        return returncode == 0

    async def remove(self, container_id: str, force: bool = False) -> bool:
        """Remove a container.

        Args:
            container_id: Container to remove
            force: Force removal

        Returns:
            True if successful
        """
        args = ["rm"]
        if force:
            args.append("-f")
        args.append(container_id)

        returncode, _, _ = await self._run_command(args, timeout=30)
        return returncode == 0

    async def remove_image(self, image: str, force: bool = False) -> bool:
        """Remove an image.

        Args:
            image: Image to remove
            force: Force removal

        Returns:
            True if successful
        """
        args = ["rmi"]
        if force:
            args.append("-f")
        args.append(image)

        returncode, _, _ = await self._run_command(args, timeout=30)
        return returncode == 0

    async def logs(self, container_id: str) -> str:
        """Get container logs.

        Args:
            container_id: Container ID

        Returns:
            Container logs
        """
        _, stdout, stderr = await self._run_command(
            ["logs", container_id],
            timeout=30,
        )
        return stdout + stderr

    async def inspect(self, container_id: str) -> dict[str, Any]:
        """Inspect a container.

        Args:
            container_id: Container ID

        Returns:
            Container info dict
        """
        _, stdout, _ = await self._run_command(
            ["inspect", container_id],
            timeout=10,
        )
        try:
            data = json.loads(stdout)
            return data[0] if data else {}
        except json.JSONDecodeError:
            return {}

    async def get_port_mapping(self, container_id: str, container_port: int) -> int | None:
        """Get the host port mapped to a container port.

        Args:
            container_id: Container ID
            container_port: Container port

        Returns:
            Host port or None
        """
        info = await self.inspect(container_id)
        ports = info.get("NetworkSettings", {}).get("Ports", {})

        port_key = f"{container_port}/tcp"
        if port_key in ports and ports[port_key]:
            return int(ports[port_key][0]["HostPort"])
        return None

    async def wait_for_health(
        self,
        container_id: str,
        config: HealthCheckConfig,
        host: str = "localhost",
    ) -> bool:
        """Wait for container to be healthy.

        Args:
            container_id: Container to check
            config: Health check configuration
            host: Host to connect to

        Returns:
            True if health check passes
        """
        # Get the mapped host port
        host_port = await self.get_port_mapping(container_id, config.port)
        if host_port is None:
            host_port = config.port

        end_time = time.time() + config.timeout

        while time.time() < end_time:
            try:
                if config.type == HealthCheckType.HTTP:
                    async with httpx.AsyncClient() as client:
                        url = f"http://{host}:{host_port}{config.path}"
                        response = await client.get(url, timeout=5.0)
                        if response.status_code < 500:
                            return True

                elif config.type == HealthCheckType.TCP:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(host, host_port),
                        timeout=5.0,
                    )
                    writer.close()
                    await writer.wait_closed()
                    return True

                elif config.type == HealthCheckType.EXEC:
                    # Check container is running
                    info = await self.inspect(container_id)
                    state = info.get("State", {})
                    if state.get("Running"):
                        return True

            except Exception:
                pass

            await asyncio.sleep(config.interval)

        return False


@asynccontextmanager
async def temp_dockerfile(content: str) -> AsyncIterator[Path]:
    """Create a temporary directory with a Dockerfile.

    Args:
        content: Dockerfile content

    Yields:
        Path to temp directory
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        dockerfile_path = Path(tmpdir) / "Dockerfile"
        dockerfile_path.write_text(content)
        yield Path(tmpdir)


@asynccontextmanager
async def running_container(
    docker: DockerClient,
    image: str,
    **kwargs: Any,
) -> AsyncIterator[str]:
    """Context manager for a running container.

    Args:
        docker: Docker client
        image: Image to run
        **kwargs: Arguments for docker.run()

    Yields:
        Container ID
    """
    container_id, error = await docker.run(image, detach=True, **kwargs)
    if error:
        raise RuntimeError(f"Failed to start container: {error}")

    try:
        yield container_id
    finally:
        await docker.stop(container_id)
        await docker.remove(container_id, force=True)


async def run_runtime_test(
    docker: DockerClient,
    image: str,
    runtime_config: RuntimeConfig,
) -> RuntimeResult:
    """Run a container and perform health check.

    Args:
        docker: Docker client
        image: Image to run
        runtime_config: Runtime configuration

    Returns:
        RuntimeResult with test outcome
    """
    if not runtime_config.enabled:
        return RuntimeResult(success=True, health_check_passed=True)

    # Determine ports to expose
    ports: dict[int, int] = {}
    if runtime_config.healthcheck.type in (HealthCheckType.HTTP, HealthCheckType.TCP):
        ports[runtime_config.healthcheck.port] = 0  # Dynamic port allocation

    container_id, error = await docker.run(
        image,
        command=runtime_config.command,
        environment=runtime_config.environment,
        ports=ports,
        detach=True,
    )

    if error:
        return RuntimeResult(
            success=False,
            errors=[f"Failed to start container: {error}"],
        )

    try:
        # Wait for container to be ready
        health_passed = await docker.wait_for_health(
            container_id,
            runtime_config.healthcheck,
        )

        logs = await docker.logs(container_id)

        return RuntimeResult(
            success=health_passed,
            container_id=container_id,
            health_check_passed=health_passed,
            logs=logs,
            errors=[] if health_passed else ["Health check failed"],
        )

    finally:
        await docker.stop(container_id)
        await docker.remove(container_id, force=True)
