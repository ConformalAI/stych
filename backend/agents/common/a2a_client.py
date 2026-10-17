"""
A2A HTTP Client for agent-to-agent communication.

This client uses the official a2a-sdk to communicate with remote agents
via HTTP streaming, following the Agent2Agent (A2A) protocol.
"""

import logging
from typing import Any, AsyncIterator, Dict, Optional

import httpx

try:
    from a2a.client import A2AClient as SDKA2AClient
    from a2a.types import Message, TaskRequest

    A2A_AVAILABLE = True
except ImportError:
    A2A_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("a2a-sdk not available. Install with: uv add a2a-sdk")

logger = logging.getLogger(__name__)


class A2AAgentClient:
    """
    HTTP client for A2A agent-to-agent communication.

    Enables distributed agents to communicate via the A2A protocol
    over HTTP, supporting both synchronous and streaming responses.
    """

    def __init__(self, timeout: int = 30):
        """
        Initialize A2A client.

        Args:
            timeout: Request timeout in seconds
        """
        if not A2A_AVAILABLE:
            raise ImportError("a2a-sdk is required. Install with: uv add 'a2a-sdk[http-server]'")

        self.timeout = timeout
        self.http_client = httpx.AsyncClient(timeout=timeout)

    async def invoke_agent(
        self,
        agent_url: str,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        stream: bool = True,
    ) -> AsyncIterator[str]:
        """
        Invoke a remote agent via A2A protocol.

        Args:
            agent_url: Base URL of the remote agent (e.g., http://localhost:8001)
            task: Task description or user message
            context: Optional context data (dataframe, conversation history, etc.)
            stream: Whether to stream the response

        Yields:
            Response chunks from the agent
        """
        try:
            # Create A2A client for the remote agent
            client = SDKA2AClient(base_url=agent_url, http_client=self.http_client)

            # Build task request
            task_request = TaskRequest(
                messages=[Message(role="user", content=task)],
                context=context or {},
            )

            if stream:
                # Stream response chunks
                async for event in client.send_task_request(task_request, stream=True):
                    if hasattr(event, "content"):
                        yield event.content
                    elif isinstance(event, dict) and "content" in event:
                        yield event["content"]
                    else:
                        yield str(event)
            else:
                # Get complete response
                response = await client.send_task_request(task_request, stream=False)
                if hasattr(response, "content"):
                    yield response.content
                elif isinstance(response, dict) and "content" in response:
                    yield response["content"]
                else:
                    yield str(response)

        except Exception as e:
            logger.error(f"Error invoking agent at {agent_url}: {e}")
            yield f"Error: {str(e)}"

    async def get_agent_card(self, agent_url: str) -> Dict[str, Any]:
        """
        Fetch agent card from remote agent.

        Args:
            agent_url: Base URL of the remote agent

        Returns:
            Agent card dictionary with capabilities and skills
        """
        try:
            response = await self.http_client.get(f"{agent_url}/.well-known/agent-card")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching agent card from {agent_url}: {e}")
            return {
                "error": str(e),
                "name": "Unknown",
                "capabilities": {},
            }

    async def close(self):
        """Close the HTTP client."""
        await self.http_client.aclose()


class AgentRegistry:
    """
    Registry mapping agent types to their service URLs.

    Enables service discovery for distributed agent deployment.
    """

    def __init__(self, config: Optional[Dict[str, str]] = None):
        """
        Initialize agent registry.

        Args:
            config: Dictionary mapping agent types to URLs
                   e.g., {"analysis": "http://localhost:8001", ...}
        """
        self.registry = config or {}
        logger.info(f"Agent registry initialized with {len(self.registry)} agents")

    def register(self, agent_type: str, url: str):
        """Register an agent with its URL."""
        self.registry[agent_type] = url
        logger.info(f"Registered {agent_type} agent at {url}")

    def get_agent_url(self, agent_type: str) -> Optional[str]:
        """Get the URL for an agent type."""
        return self.registry.get(agent_type)

    def list_agents(self) -> Dict[str, str]:
        """List all registered agents."""
        return self.registry.copy()
