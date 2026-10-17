import json
from typing import Any, Dict


class CleaningExporter:
    def __init__(self, agent: Any):
        self.agent = agent

    async def generate_export_readme(
        self,
        *,
        code: str,
        plan: Dict[str, Any],
        choices: Dict[str, str],
        dataset_summary: Dict[str, Any],
    ) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "You write concise README files for data-cleaning scripts. "
                    "Keep it short, practical, and focused on how to run the script. "
                    "Use Markdown headings and bullet points. Avoid code fences."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Generate a README.md for a cleaning script with:\n"
                    f"- Dataset summary: {json.dumps(dataset_summary, default=str)}\n"
                    f"- Cleaning plan: {json.dumps(plan, default=str)}\n"
                    f"- User choices: {json.dumps(choices, default=str)}\n"
                    "Include sections:\n"
                    "1) Overview\n"
                    "2) What the script does\n"
                    "3) How to run\n"
                    "4) Output\n"
                    "5) Notes / assumptions\n"
                    "Keep it under 200 words."
                ),
            },
        ]
        readme = (await self.agent.generate_response(messages, temperature=0.2)).strip()
        return readme

    async def generate_export_readme_sql(
        self,
        *,
        sql: str,
        plan: Dict[str, Any],
        choices: Dict[str, str],
        dataset_summary: Dict[str, Any],
    ) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "You write concise README files for SQL cleaning scripts. "
                    "Keep it short, practical, and focused on how to run the SQL safely. "
                    "Use Markdown headings and bullet points. Avoid code fences."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Generate a README.md for a SQL cleaning script with:\n"
                    f"- Dataset summary: {json.dumps(dataset_summary, default=str)}\n"
                    f"- Cleaning plan: {json.dumps(plan, default=str)}\n"
                    f"- User choices: {json.dumps(choices, default=str)}\n"
                    "Include sections:\n"
                    "1) Overview\n"
                    "2) What the SQL does\n"
                    "3) How to run\n"
                    "4) Output\n"
                    "5) Notes / assumptions\n"
                    "Keep it under 200 words."
                ),
            },
        ]
        readme = (await self.agent.generate_response(messages, temperature=0.2)).strip()
        return readme
