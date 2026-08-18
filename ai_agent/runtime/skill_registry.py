import os
import json
import hashlib
import frappe
from frappe import _
from ai_agent.runtime.constants import SKILL_INSTRUCTIONS_DIR
from ai_agent.runtime.exceptions import SkillNotFound, SkillDenied


SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), SKILL_INSTRUCTIONS_DIR)


class SkillRegistry:
    """Manages skill discovery, loading, and grant resolution."""

    def __init__(self):
        self._metadata_cache = {}
        self._permission_cache = {}

    def list_effective_skills(self, user, agent_name, company=None):
        """List skills available to a user for a given agent."""
        cache_key = f"{user}:{agent_name}:{company or ''}"
        if cache_key in self._permission_cache:
            return self._permission_cache[cache_key]

        grants = self._get_matching_grants(user, agent_name, company)
        skills = []
        seen = set()
        for grant in grants:
            skill_name = grant.skill
            if skill_name in seen:
                continue
            seen.add(skill_name)
            skill_doc = frappe.get_doc("AI Skill", skill_name)
            if not skill_doc or not skill_doc.enabled:
                continue
            meta = self._get_skill_metadata(skill_name)
            if meta:
                skills.append(meta)

        self._permission_cache[cache_key] = skills
        return skills

    def get_skill_metadata(self, skill_name, user, agent_name, company=None):
        """Get metadata for a specific skill."""
        return self._get_skill_metadata(skill_name)

    def load_skill(self, skill_name, user, agent_name, company=None):
        """Load full skill instructions and tool list."""
        if not self._is_skill_granted(skill_name, user, agent_name, company):
            raise SkillDenied(f"Skill '{skill_name}' is not granted to this user/agent.")

        skill_doc = frappe.get_doc("AI Skill", skill_name)
        if not skill_doc or not skill_doc.enabled:
            raise SkillNotFound(f"Skill '{skill_name}' not found or disabled.")

        instructions = ""
        if skill_doc.source_type == "App File" and skill_doc.skill_path:
            instructions = self._read_app_file_skill(skill_doc.skill_path)
        elif skill_doc.source_type == "Database":
            instructions = skill_doc.instructions or ""

        tools = []
        for tool_row in skill_doc.tools:
            tools.append({
                "tool_name": tool_row.tool_name,
                "access_level": tool_row.access_level,
                "requires_approval": tool_row.requires_approval,
                "maximum_calls": tool_row.maximum_calls,
                "maximum_result_rows": tool_row.get("maximum_result_rows"),
            })

        return {
            "name": skill_name,
            "title": skill_doc.title,
            "description": skill_doc.description,
            "instructions": instructions,
            "tools": tools,
            "version": skill_doc.version,
            "runtime_policy": self._runtime_policy(skill_doc),
        }

    def sync_app_skills(self):
        """Scan the skills directory and sync SKILL.md files to AI Skill DocType."""
        if not os.path.isdir(SKILLS_DIR):
            return

        for entry in os.listdir(SKILLS_DIR):
            skill_dir = os.path.join(SKILLS_DIR, entry)
            if not os.path.isdir(skill_dir):
                continue
            skill_file = os.path.join(skill_dir, "SKILL.md")
            if not os.path.isfile(skill_file):
                continue

            content = self._read_skill_file(skill_file)
            meta = self._parse_frontmatter(content)
            if not meta.get("name"):
                continue

            skill_name = meta["name"]
            description = meta.get("description", "")
            allow_implicit_invocation = self._as_bool(meta.get("allow_implicit_invocation"))
            instructions = self._extract_instructions(content)
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            declared_tools = self._parse_csv(meta.get("tools")) if "tools" in meta else None
            tool_call_limits = self._parse_int_mapping(meta.get("tool_call_limits"))
            tool_result_limits = self._parse_int_mapping(meta.get("tool_result_limits"))

            existing = frappe.db.exists("AI Skill", skill_name)
            if existing:
                skill_doc = frappe.get_doc("AI Skill", skill_name)
                if skill_doc.source_type == "Database":
                    continue
                if skill_doc.content_hash == content_hash:
                    continue
                changed = False
                standard_values = {
                    "title": skill_name.replace("-", " ").title(),
                    "description": description,
                    "source_type": "App File",
                    "skill_path": entry,
                    "instructions": instructions,
                    "enabled": 1,
                    "allow_implicit_invocation": allow_implicit_invocation,
                    "trusted": 1,
                    "content_hash": content_hash,
                    "response_format": self._normalize_response_format(meta.get("response_format")),
                    "required_output_keys": ",".join(self._parse_csv(meta.get("required_output_keys"))),
                    "finalization_max_tokens": self._positive_int(meta.get("finalization_max_tokens")),
                    "finalization_timeout": self._positive_int(meta.get("finalization_timeout")),
                    "final_output_instruction": meta.get("final_output_instruction", ""),
                }
                for field, value in standard_values.items():
                    if skill_doc.get(field) != value:
                        skill_doc.set(field, value)
                        changed = True
                if declared_tools is not None and self._sync_declared_tools(
                    skill_doc,
                    declared_tools,
                    tool_call_limits,
                    tool_result_limits,
                ):
                    changed = True
                if changed:
                    skill_doc.save(ignore_permissions=True)
            else:
                skill_doc = frappe.get_doc({
                    "doctype": "AI Skill",
                    "skill_name": skill_name,
                    "title": skill_name.replace("-", " ").title(),
                    "description": description,
                    "source_type": "App File",
                    "skill_path": entry,
                    "instructions": instructions,
                    "enabled": 1,
                    "allow_implicit_invocation": allow_implicit_invocation,
                    "trusted": 1,
                    "content_hash": content_hash,
                    "response_format": self._normalize_response_format(meta.get("response_format")),
                    "required_output_keys": ",".join(self._parse_csv(meta.get("required_output_keys"))),
                    "finalization_max_tokens": self._positive_int(meta.get("finalization_max_tokens")),
                    "finalization_timeout": self._positive_int(meta.get("finalization_timeout")),
                    "final_output_instruction": meta.get("final_output_instruction", ""),
                })
                if declared_tools is not None:
                    self._sync_declared_tools(
                        skill_doc,
                        declared_tools,
                        tool_call_limits,
                        tool_result_limits,
                    )
                skill_doc.insert(ignore_permissions=True)
        frappe.db.commit()

    def _get_skill_metadata(self, skill_name):
        if skill_name in self._metadata_cache:
            return self._metadata_cache[skill_name]

        try:
            skill_doc = frappe.get_doc("AI Skill", skill_name)
        except frappe.DoesNotExistError:
            return None

        meta = {
            "name": skill_name,
            "title": skill_doc.title,
            "description": skill_doc.description,
            "enabled": skill_doc.enabled,
            "allow_implicit_invocation": skill_doc.allow_implicit_invocation,
            "instructions": skill_doc.instructions if skill_doc.allow_implicit_invocation else "",
            "tools": [
                {
                    "tool_name": row.tool_name,
                    "access_level": row.access_level,
                    "requires_approval": row.requires_approval,
                    "maximum_calls": row.maximum_calls,
                    "maximum_result_rows": row.get("maximum_result_rows"),
                }
                for row in (skill_doc.tools or [])
            ],
            "runtime_policy": self._runtime_policy(skill_doc),
        }
        self._metadata_cache[skill_name] = meta
        return meta

    def _as_bool(self, value):
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}

    def _parse_csv(self, value):
        return [item.strip() for item in str(value or "").split(",") if item.strip()]

    def _positive_int(self, value):
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            return 0
        return max(number, 0)

    def _parse_int_mapping(self, value):
        result = {}
        for item in self._parse_csv(value):
            name, separator, raw_limit = item.partition("=")
            name = name.strip()
            if not separator or not name:
                continue
            limit = self._positive_int(raw_limit)
            if limit:
                result[name] = limit
        return result

    def _normalize_response_format(self, value):
        normalized = str(value or "").strip().lower().replace("_", " ")
        if normalized in {"json", "json object", "jsonobject"}:
            return "JSON Object"
        if normalized == "text":
            return "Text"
        return ""

    def _sync_declared_tools(self, skill_doc, tool_names, call_limits=None, result_limits=None):
        call_limits = call_limits or {}
        result_limits = result_limits or {}
        desired = [
            (
                tool_name,
                self._positive_int(call_limits.get(tool_name)),
                self._positive_int(result_limits.get(tool_name)),
            )
            for tool_name in tool_names
        ]
        existing = [
            (
                row.tool_name,
                self._positive_int(row.maximum_calls),
                self._positive_int(row.get("maximum_result_rows")),
            )
            for row in (skill_doc.tools or [])
        ]
        if existing == desired:
            return False
        skill_doc.set("tools", [])
        for tool_name, maximum_calls, maximum_result_rows in desired:
            skill_doc.append("tools", {
                "tool_name": tool_name,
                "access_level": "Read",
                "requires_approval": 0,
                "maximum_calls": maximum_calls,
                "maximum_result_rows": maximum_result_rows,
            })
        return True

    def _runtime_policy(self, skill_doc):
        return {
            "response_format": skill_doc.get("response_format") or "",
            "required_output_keys": self._parse_csv(skill_doc.get("required_output_keys")),
            "finalization_max_tokens": self._positive_int(skill_doc.get("finalization_max_tokens")),
            "finalization_timeout": self._positive_int(skill_doc.get("finalization_timeout")),
            "final_output_instruction": str(skill_doc.get("final_output_instruction") or "").strip(),
        }

    def _get_matching_grants(self, user, agent_name, company=None):
        """Find all grants matching user/agent/company."""
        conditions = {"enabled": 1, "agent": agent_name}
        grants = frappe.get_all("AI Skill Grant", filters=conditions, fields=["*"])

        matched = []
        user_roles = set(frappe.get_roles(user))

        for grant in grants:
            if grant.grant_type == "All Users":
                matched.append(grant)
            elif grant.grant_type == "Role" and grant.role in user_roles:
                matched.append(grant)
            elif grant.grant_type == "User" and grant.user == user:
                matched.append(grant)

            if company and grant.company and grant.company != company:
                if grant in matched:
                    matched.remove(grant)

            if grant.valid_from and grant.valid_until:
                now = frappe.utils.now_datetime()
                if now < grant.valid_from or now > grant.valid_until:
                    if grant in matched:
                        matched.remove(grant)

        return matched

    def _is_skill_granted(self, skill_name, user, agent_name, company=None):
        grants = self._get_matching_grants(user, agent_name, company)
        return any(g.skill == skill_name for g in grants)

    def _read_app_file_skill(self, skill_path):
        safe_path = os.path.normpath(skill_path)
        if ".." in safe_path:
            return ""
        full_path = os.path.join(SKILLS_DIR, safe_path, "SKILL.md")
        if not os.path.isfile(full_path):
            return ""
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()

    def _read_skill_file(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def _parse_frontmatter(self, content):
        if not content.startswith("---"):
            return {}
        try:
            end = content.index("---", 3)
            block = content[3:end].strip()
            result = {}
            for line in block.split("\n"):
                if ":" in line:
                    key, val = line.split(":", 1)
                    result[key.strip()] = val.strip()
            return result
        except (ValueError, IndexError):
            return {}

    def _extract_instructions(self, content):
        if content.startswith("---"):
            try:
                end = content.index("---", 3)
                return content[end + 3:].strip()
            except ValueError:
                pass
        return content.strip()

    def clear_cache(self, user=None, agent_name=None):
        """Clear permission and metadata caches."""
        self._permission_cache.clear()
        if user or agent_name:
            keys_to_remove = [k for k in self._permission_cache if k.startswith(f"{user or ''}:{agent_name or ''}")]
            for k in keys_to_remove:
                del self._permission_cache[k]
