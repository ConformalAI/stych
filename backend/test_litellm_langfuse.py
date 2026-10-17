"""
Test script for LiteLLM and Langfuse integration.
Run this to verify the setup is working correctly.
"""

import asyncio
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

from llm.infer import LLMRequest, LLMRouter, Message

load_dotenv()


async def test_litellm_provider():
    """Test LiteLLM provider connectivity."""
    print("\n" + "=" * 60)
    print("Testing LiteLLM Provider")
    print("=" * 60)

    config = {
        "provider": "litellm",
        "litellm": {
            "base_url": "http://localhost:4000",
            "api_key": "dummy-key",
            "default_model": "gpt-4",
        },
    }

    router = LLMRouter(config)

    test_request = LLMRequest(
        messages=[Message(role="user", content="Say 'Hello from LiteLLM!'")],
        max_tokens=50,
        temperature=0.5,
        metadata={
            "session_id": "test-session",
            "agent_name": "TestAgent",
        },
    )

    try:
        print("\n📤 Sending request to LiteLLM...")
        response = await router.generate(test_request)

        print("✅ Success!")
        print(f"   Model: {response.model}")
        print(f"   Content: {response.content[:100]}...")
        print(f"   Usage: {response.usage}")
        print(f"   Provider: {response.metadata.get('provider')}")

        return True

    except Exception as e:
        print(f"❌ Failed: {e}")
        return False


async def test_langfuse_tracking():
    """Test Langfuse tracking configuration."""
    print("\n" + "=" * 60)
    print("Testing Langfuse Tracking")
    print("=" * 60)

    config = {
        "langfuse": {
            "enabled": True,
            "public_key": "pk-test-key",
            "secret_key": "sk-test-key",
            "host": "http://localhost:3110",
        }
    }

    from llm.langfuse_client import get_langfuse_tracker, init_langfuse_tracker

    init_langfuse_tracker(config)
    tracker = get_langfuse_tracker()

    if tracker:
        print("\n✅ Langfuse tracker initialized")
        print(f"   Enabled: {tracker.enabled}")

        # Test logging an LLM call
        tracker.log_llm_call(
            agent_name="TestAgent",
            model="gpt-4",
            provider="litellm",
            input_messages=[{"role": "user", "content": "test"}],
            output="Test response",
            latency_ms=1234.5,
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            session_id="test-session",
        )

        print("✅ Test LLM call logged to Langfuse")
        tracker.flush()
        print("✅ Langfuse flushed")

        return True
    else:
        print("❌ Langfuse tracker not initialized (check configuration)")
        return False


async def test_provider_fallback():
    """Test provider fallback mechanism."""
    print("\n" + "=" * 60)
    print("Testing Provider Fallback")
    print("=" * 60)

    config = {
        "provider": "litellm",
        "litellm": {
            "base_url": "http://localhost:4000",
            "api_key": "dummy-key",
            "default_model": "gpt-4",
            "fallback_models": ["claude-3.5-sonnet", "gemini-1.5-pro"],
        },
    }

    router = LLMRouter(config)

    test_request = LLMRequest(
        messages=[Message(role="user", content="Test fallback")],
        max_tokens=50,
        temperature=0.5,
    )

    try:
        print("\n📤 Testing primary model...")
        response = await router.generate(test_request)
        print(f"✅ Success with model: {response.model}")
        return True

    except Exception as e:
        print(f"❌ Failed (would try fallback): {e}")
        return False


async def test_openrouter_provider():
    """Test OpenRouter provider (for comparison)."""
    print("\n" + "=" * 60)
    print("Testing OpenRouter Provider (for comparison)")
    print("=" * 60)

    config = {
        "provider": "openrouter",
        "openrouter": {
            "api_key": "sk-or-test-key",  # Use your real key
            "default_model": "anthropic/claude-3.5-sonnet",
        },
    }

    router = LLMRouter(config)

    test_request = LLMRequest(
        messages=[Message(role="user", content="Say 'Hello from OpenRouter!'")],
        max_tokens=50,
        temperature=0.5,
    )

    try:
        print("\n📤 Sending request to OpenRouter...")
        response = await router.generate(test_request)

        print("✅ Success!")
        print(f"   Model: {response.model}")
        print(f"   Content: {response.content[:100]}...")
        print(f"   Provider: {response.metadata.get('provider')}")

        return True

    except Exception as e:
        print(f"❌ Failed: {e}")
        return False


async def run_all_tests():
    """Run all tests."""
    print("\n" + "🚀" * 30)
    print("Stych LLM Integration Test Suite")
    print("🚀" * 30)

    results = {}

    # Test LiteLLM
    results["litellm"] = await test_litellm_provider()

    # Test Langfuse
    results["langfuse"] = await test_langfuse_tracking()

    # Test fallback
    results["fallback"] = await test_provider_fallback()

    # Test OpenRouter (optional - requires API key)
    # Uncomment to test:
    # results["openrouter"] = await test_openrouter_provider()

    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)

    for test_name, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{test_name:20} {status}")

    total_tests = len(results)
    passed_tests = sum(results.values())

    print(f"\nTotal: {passed_tests}/{total_tests} tests passed")

    if passed_tests == total_tests:
        print("\n🎉 All tests passed! Integration is working correctly.")
    else:
        print("\n⚠️  Some tests failed. Check the errors above.")

    print("\n" + "=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(run_all_tests())
