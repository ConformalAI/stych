"""
A2A Protocol implementation with Google ADK agents.
Defines A2A server setup and protocol handlers for agent-to-agent communication.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    from a2a.models import (
        AgentCard,
        TaskRequest,
        TaskResponse,
        TaskStatus,
    )
    from a2a.server import A2AServer

    A2A_AVAILABLE = True
except ImportError:
    logger.warning("a2a package not installed. A2A functionality will be disabled.")
    A2A_AVAILABLE = False


@dataclass
class A2AConfig:
    """A2A configuration."""

    enabled: bool
    agent_id: str
    agent_name: str
    description: str
    version: str
    capabilities: List[str]
    host: str = "0.0.0.0"
    port: int = 8001


class StychA2AProtocol:
    """
    Stych A2A Protocol handler.
    Exposes Stych agents via A2A protocol for agent-to-agent communication.
    """

    def __init__(self, config: A2AConfig, agent_executor):
        """
        Initialize A2A protocol handler.

        Args:
            config: A2A configuration
            agent_executor: Agent executor instance to handle requests
        """
        self.config = config
        self.agent_executor = agent_executor
        self.server: Optional[Any] = None

        if not A2A_AVAILABLE:
            logger.warning("A2A SDK not available. Protocol will not be initialized.")
            return

        self._initialize_server()

    def _initialize_server(self):
        """Initialize A2A server with agent card."""
        if not A2A_AVAILABLE:
            return

        # Create agent card
        agent_card = AgentCard(
            id=self.config.agent_id,
            name=self.config.agent_name,
            description=self.config.description,
            version=self.config.version,
            capabilities=self.config.capabilities,
            # Add supported data formats
            supported_formats=[
                "text/plain",
                "text/csv",
                "application/json",
            ],
            # Add metadata
            metadata={
                "framework": "google-adk",
                "llm_provider": "openrouter",
            },
        )

        # Create A2A server
        self.server = A2AServer(
            agent_card=agent_card,
            task_handler=self._handle_task,
        )

        logger.info(f"A2A server initialized: {self.config.agent_name}")

    async def _handle_task(self, task_request: "TaskRequest") -> "TaskResponse":
        """
        Handle incoming A2A task request.

        Args:
            task_request: A2A task request

        Returns:
            Task response with result
        """
        try:
            logger.info(f"Received A2A task: {task_request.task_id}")

            # Extract task details
            task_data = task_request.data
            task_type = task_data.get("type", "query")
            user_message = task_data.get("message", "")
            context = task_data.get("context", {})

            # Execute agent task
            result = await self._execute_agent_task(
                task_type=task_type,
                message=user_message,
                context=context,
                task_id=task_request.task_id,
            )

            # Return success response
            return TaskResponse(
                task_id=task_request.task_id,
                status=TaskStatus.COMPLETED,
                result=result,
                metadata={
                    "agent_id": self.config.agent_id,
                    "task_type": task_type,
                },
            )

        except Exception as e:
            logger.error(f"Error handling A2A task: {e}")
            return TaskResponse(
                task_id=task_request.task_id,
                status=TaskStatus.FAILED,
                error=str(e),
            )

    async def _execute_agent_task(
        self,
        task_type: str,
        message: str,
        context: Dict[str, Any],
        task_id: str,
    ) -> Dict[str, Any]:
        """
        Execute agent task internally.

        Args:
            task_type: Type of task (analysis, cleaning, qa)
            message: User message/query
            context: Additional context
            task_id: Task identifier

        Returns:
            Task execution result
        """
        # Use agent executor to handle the task
        result = await self.agent_executor.execute(
            task_type=task_type,
            message=message,
            context=context,
        )

        return {
            "response": result.get("response", ""),
            "data": result.get("data", {}),
            "metadata": {
                "task_id": task_id,
                "agent": self.config.agent_id,
            },
        }

    async def start(self):
        """Start A2A server."""
        if not self.config.enabled or not A2A_AVAILABLE:
            logger.info("A2A server disabled or unavailable")
            return

        if self.server:
            logger.info(f"Starting A2A server on {self.config.host}:{self.config.port}")
            await self.server.start(host=self.config.host, port=self.config.port)

    async def stop(self):
        """Stop A2A server."""
        if self.server:
            logger.info("Stopping A2A server")
            await self.server.stop()

    def get_agent_card(self) -> Optional[Dict[str, Any]]:
        """Get agent card for discovery."""
        if not A2A_AVAILABLE:
            return None

        return {
            "id": self.config.agent_id,
            "name": self.config.agent_name,
            "description": self.config.description,
            "version": self.config.version,
            "capabilities": self.config.capabilities,
            "endpoint": f"http://{self.config.host}:{self.config.port}",
        }


class A2AClient:
    """
    Client for communicating with remote A2A agents.
    """

    def __init__(self):
        self.remote_agents: Dict[str, Any] = {}

    async def register_remote_agent(self, agent_url: str, agent_id: Optional[str] = None):
        """
        Register a remote A2A agent.

        Args:
            agent_url: URL of the remote agent
            agent_id: Optional agent ID
        """
        if not A2A_AVAILABLE:
            logger.warning("A2A SDK not available. Cannot register remote agent.")
            return

        try:
            # Fetch agent card from remote agent
            from a2a.client import A2AClient as SDK_Client

            client = SDK_Client(agent_url)
            agent_card = await client.get_agent_card()

            agent_id = agent_id or agent_card.id
            self.remote_agents[agent_id] = {
                "url": agent_url,
                "client": client,
                "card": agent_card,
            }

            logger.info(f"Registered remote agent: {agent_id} at {agent_url}")

        except Exception as e:
            logger.error(f"Failed to register remote agent {agent_url}: {e}")

    async def call_remote_agent(
        self,
        agent_id: str,
        task_data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Call a remote A2A agent.

        Args:
            agent_id: ID of the remote agent
            task_data: Task data to send

        Returns:
            Task response from remote agent
        """
        if agent_id not in self.remote_agents:
            logger.error(f"Remote agent {agent_id} not registered")
            return None

        try:
            client = self.remote_agents[agent_id]["client"]

            # Create task request
            from a2a.models import TaskRequest

            task_request = TaskRequest(data=task_data)

            # Send request and get response
            response = await client.send_task(task_request)

            return {
                "status": response.status.value,
                "result": response.result,
                "error": response.error,
            }

        except Exception as e:
            logger.error(f"Error calling remote agent {agent_id}: {e}")
            return None

    def list_remote_agents(self) -> List[Dict[str, Any]]:
        """List all registered remote agents."""
        return [
            {
                "id": agent_id,
                "url": info["url"],
                "capabilities": info["card"].capabilities if info.get("card") else [],
            }
            for agent_id, info in self.remote_agents.items()
        ]
