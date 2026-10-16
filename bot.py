import os
import sys
import json
import subprocess
import urllib.request
import urllib.error
import threading
import time
import asyncio
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
import discord
from discord.ext import commands

load_dotenv()

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID")  # Only allow this user
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GH_PAT = os.environ.get("GH_PAT")
GH_USERNAME = os.environ.get("GH_USERNAME", "harshitaditya")
DISCORD_USERNAME = os.environ.get("DISCORD_USERNAME", "aditya_ak02")

# All supported AI Models (Active & Tested)
ALL_MODELS = {
    "gemini-3.5-flash-lite": "⚡ Gemini 3.5 Flash Lite (Ultra Fast & 0% Throttling) [DEFAULT]",
    "gemini-3.5-flash": "🚀 Gemini 3.5 Flash (Flagship Speed & Capacity)",
    "gemini-3.6-flash": "⚡ Gemini 3.6 Flash (Super Fast & Smart)",
    "gemini-3-flash-preview": "🛡️ Gemini 3 Flash Preview (Rock Solid Fallback)",
    "gemini-3.8-flash": "⚡ Gemini 3.8 Flash (Latest Flash)",
    "gemini-3.1-pro": "🧠 Gemini 3.1 Pro (Deep Logic & Complex Architecture)",
    "gemini-4-argon": "🔮 Gemini 4 Argon (Next-Gen Autonomous Agentic Engine)",
    "claude-sonnet-4-6": "🎭 Claude Sonnet 4.6 (Elite Coding & Refactoring)",
    "claude-opus-4-6": "👑 Claude Opus 4.6 (Deep Architectural Reasoning)",
}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# User state storage
user_sessions = {}

def get_session(user_id):
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "active_repo": None,
            "active_branch": "main",
            "active_model": "gemini-3.5-flash-lite",
            "pending_summary": None,
            "pending_files": [],
            "work_dir": None,
            "chat_history": [],
            "last_suggested_task": None
        }
    if "chat_history" not in user_sessions[user_id]:
        user_sessions[user_id]["chat_history"] = []
    if "last_suggested_task" not in user_sessions[user_id]:
        user_sessions[user_id]["last_suggested_task"] = None
    return user_sessions[user_id]

def is_authorized(user):
    """Check if the user is authorized by username or user ID (case-insensitive)."""
    if not DISCORD_USER_ID and not DISCORD_USERNAME:
        return True
    user_str_id = str(getattr(user, "id", user))
    user_name = str(getattr(user, "name", ""))
    display_name = str(getattr(user, "display_name", ""))
    global_name = str(getattr(user, "global_name", ""))

    if DISCORD_USER_ID and user_str_id == str(DISCORD_USER_ID):
        return True
    if DISCORD_USERNAME:
        target = DISCORD_USERNAME.strip().lower()
        if (user_name.lower() == target or 
            display_name.lower() == target or 
            global_name.lower() == target):
            return True

    print(f"⚠️ Unauthorized attempt: id={user_str_id}, name='{user_name}', display='{display_name}', global='{global_name}' (expected: {DISCORD_USERNAME})", flush=True)
    return False

# --- GitHub Helpers ---
def fetch_user_repos():
    """Fetch all repositories for the user via GitHub API."""
    url = f"https://api.github.com/users/{GH_USERNAME}/repos?per_page=100&sort=updated"
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GH_PAT:
        headers["Authorization"] = f"token {GH_PAT}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return [r["name"] for r in data]
    except Exception as e:
        print(f"Error fetching repos from GitHub: {e}", flush=True)
        return []

def gather_repo_files(repo_dir, task_query="", max_files=35, max_total_bytes=45_000):
    """Recursively collect source files prioritized by task relevance to prevent API 503 throttling."""
    file_map = {}
    ignored_dirs = {".git", "node_modules", "vendor", "__pycache__", ".vscode", "dist", "build", ".next", ".cache", "venv", ".idea"}
    ignored_exts = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".zip", ".tar", ".gz", ".lock", ".woff", ".woff2", ".ttf", ".pdf", ".exe", ".bin", ".map", ".log"}
    ignored_files = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "composer.lock", "Cargo.lock", "catalogBuildCache.json"}

    task_words = set(task_query.lower().split()) if task_query else set()
    is_styling = any(w in task_query.lower() for w in ["css", "style", "design", "ui", "theme", "color", "layout"])

    all_paths = []
    candidates = []

    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, repo_dir)
            all_paths.append(rel_path)

            if f in ignored_files or f.endswith(".lock"):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in ignored_exts:
                continue
            if ext == ".css" and not is_styling and os.path.getsize(full_path) > 10_000:
                continue

            try:
                size = os.path.getsize(full_path)
                if size > 60_000:
                    continue

                path_lower = rel_path.lower()
                score = 0
                for w in task_words:
                    if len(w) > 2 and w in path_lower:
                        score += 10
                if ext in [".js", ".jsx", ".ts", ".tsx", ".py", ".json", ".html"]:
                    score += 1
                candidates.append((score, rel_path, full_path, size))
            except Exception:
                pass

    candidates.sort(key=lambda x: (-x[0], x[3]))

    total_bytes = 0
    for score, rel_path, full_path, size in candidates:
        if len(file_map) >= max_files:
            break
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as fp:
                content = fp.read()
            if total_bytes + len(content) <= max_total_bytes:
                file_map[rel_path] = content
                total_bytes += len(content)
            elif len(file_map) == 0:
                file_map[rel_path] = content[:max_total_bytes]
                total_bytes += len(file_map[rel_path])
                break
        except Exception:
            pass

    return file_map, all_paths

MODEL_ALIASES = {
    "gemini-3.1-pro": "gemini-3.1-pro-preview",
    "gemini-2.5-pro": "gemini-3.6-flash",
    "gemini-2.5-flash": "gemini-3.6-flash",
    "gemini-2.0-flash": "gemini-3.6-flash",
    "gemini-1.5-pro": "gemini-3.6-flash",
    "gemini-1.5-flash": "gemini-3.6-flash",
}

def enrich_task_with_external_links(task_text):
    """If task contains a Spotify playlist or web link, extract track names or page text to augment prompt."""
    extra_context = ""
    # Check for Spotify playlist
    spotify_playlist_match = re.search(r"https?://open\.spotify\.com/playlist/([a-zA-Z0-9]+)", task_text)
    if spotify_playlist_match:
        playlist_id = spotify_playlist_match.group(1)
        embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
        req = urllib.request.Request(embed_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                html = r.read().decode("utf-8", errors="ignore")
                m = re.findall(r"<script id=\"__NEXT_DATA__\" type=\"application/json\">(.*?)</script>", html)
                if m:
                    d = json.loads(m[0])
                    tracks = d.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {}).get("trackList", [])
                    if tracks:
                        song_items = []
                        for t in tracks:
                            title = t.get("title", "").strip()
                            artists = ", ".join([a.get("name", "").strip() for a in t.get("artists", []) if a.get("name")])
                            if title:
                                song_items.append(f"- {title} (Artists: {artists})")
                        extra_context += f"\n\n[EXTRACTED SPOTIFY PLAYLIST TRACKS ({len(song_items)} songs)]:\n" + "\n".join(song_items)
                        print(f"✅ Extracted {len(song_items)} songs from Spotify playlist {playlist_id}", flush=True)
        except Exception as e:
            print(f"⚠️ Failed to fetch Spotify playlist: {e}", flush=True)

    return task_text + extra_context

def call_gemini(prompt, system_instruction, model_name="gemini-3.5-flash-lite"):
    mapped_target = MODEL_ALIASES.get(model_name, model_name)
    active_fallbacks = [
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3-flash-preview",
        "gemini-3.6-flash",
        "gemini-flash-lite-latest",
        "gemini-3.8-flash",
    ]
    models_to_try = [mapped_target] + [m for m in active_fallbacks if m != mapped_target]
    last_err = None

    for m in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text_response = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                if text_response.startswith("```json"):
                    text_response = text_response[7:]
                if text_response.startswith("```"):
                    text_response = text_response[3:]
                if text_response.endswith("```"):
                    text_response = text_response[:-3]
                text_response = text_response.strip()
                return json.loads(text_response), m
        except Exception as e:
            print(f"Error calling {m}: {e}", flush=True)
            last_err = e
            time.sleep(1)

    raise RuntimeError(f"Gemini API error. Last error: {last_err}")

def call_anthropic(prompt, system_instruction, model_name="claude-3-7-sonnet-20250219"):
    url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": model_name,
        "max_tokens": 8192,
        "system": system_instruction,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        text = data["content"][0]["text"].strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return json.loads(text.strip()), model_name

def call_ai(prompt, system_instruction, model_name="gemini-3.5-flash-lite"):
    """Unified AI caller supporting Gemini and Claude models."""
    if model_name in ["claude-sonnet-4-6", "claude-opus-4-6"]:
        if ANTHROPIC_API_KEY:
            target_claude = "claude-3-7-sonnet-20250219" if model_name == "claude-sonnet-4-6" else "claude-3-opus-20240229"
            try:
                return call_anthropic(prompt, system_instruction, target_claude)
            except Exception as e:
                print(f"Anthropic API error: {e}, falling back to Gemini...", flush=True)

        persona = (
            f"You are {model_name}, an elite software engineer. "
            "Think deeply and generate clean, bug-free production-grade modifications. "
        )
        return call_gemini(prompt, persona + system_instruction, "gemini-3.5-flash-lite")

    return call_gemini(prompt, system_instruction, model_name)

def call_gemini_text(prompt, system_instruction, model_name="gemini-3.5-flash-lite", chat_history=None):
    mapped_target = MODEL_ALIASES.get(model_name, model_name)
    active_fallbacks = [
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3-flash-preview",
        "gemini-3.6-flash",
        "gemini-flash-lite-latest",
        "gemini-3.8-flash",
    ]
    models_to_try = [mapped_target] + [m for m in active_fallbacks if m != mapped_target]
    last_err = None

    contents = []
    if chat_history:
        for turn in chat_history[-6:]:
            role = "user" if turn.get("role") == "user" else "model"
            contents.append({
                "role": role,
                "parts": [{"text": turn.get("text", "")}]
            })
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    for m in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "generationConfig": {
                "temperature": 0.4,
                "maxOutputTokens": 2048
            }
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=35) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text_response = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                return text_response, m
        except Exception as e:
            print(f"Error calling {m} for text: {e}", flush=True)
            last_err = e
            time.sleep(1)

    raise RuntimeError(f"Gemini API chat error. Last error: {last_err}")

def call_anthropic_text(prompt, system_instruction, model_name="claude-3-7-sonnet-20250219", chat_history=None):
    url = "https://api.anthropic.com/v1/messages"
    messages = []
    if chat_history:
        for turn in chat_history[-6:]:
            role = "user" if turn.get("role") == "user" else "assistant"
            messages.append({"role": role, "content": turn.get("text", "")})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model_name,
        "max_tokens": 4096,
        "system": system_instruction,
        "messages": messages,
        "temperature": 0.4
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        text = data["content"][0]["text"].strip()
        return text, model_name

def call_ai_text(prompt, system_instruction, model_name="gemini-3.5-flash-lite", chat_history=None):
    """Unified conversational AI caller returning markdown text."""
    if model_name in ["claude-sonnet-4-6", "claude-opus-4-6"]:
        if ANTHROPIC_API_KEY:
            target_claude = "claude-3-7-sonnet-20250219" if model_name == "claude-sonnet-4-6" else "claude-3-opus-20240229"
            try:
                return call_anthropic_text(prompt, system_instruction, target_claude, chat_history)
            except Exception as e:
                print(f"Anthropic text error: {e}, falling back to Gemini...", flush=True)

        persona = (
            f"You are {model_name}, a world-class senior staff software engineer and pair programmer. "
            "Help the user inspect their repository, design solutions, recommend architecture decisions, and write code.\n"
        )
        return call_gemini_text(prompt, persona + system_instruction, "gemini-3.5-flash-lite", chat_history)

    return call_gemini_text(prompt, system_instruction, model_name, chat_history)

def ensure_cloned_repo(user_id, repo, branch="main"):
    base_dir = os.path.expanduser("~/.gemini/antigravity-ide/scratch/discord_workspaces")
    os.makedirs(base_dir, exist_ok=True)
    work_dir = os.path.join(base_dir, f"{user_id}_{repo}")
    if os.path.exists(work_dir) and os.path.exists(os.path.join(work_dir, ".git")):
        return work_dir
    if GH_PAT:
        clone_url = f"https://{GH_USERNAME}:{GH_PAT}@github.com/{GH_USERNAME}/{repo}.git"
    else:
        clone_url = f"https://github.com/{GH_USERNAME}/{repo}.git"
    subprocess.run(["rm", "-rf", work_dir])
    res = subprocess.run(["git", "clone", "--depth", "1", "-b", branch, clone_url, work_dir], capture_output=True, text=True)
    if res.returncode != 0:
        subprocess.run(["git", "clone", "--depth", "1", clone_url, work_dir], capture_output=True)
    return work_dir

def create_agent_progress_embed(repo, model_label, task, step_num, step_title, percent, steps_history, current_thought=""):
    """
    Renders a dynamic live progress embed on Discord mobile.
    """
    if percent < 30:
        color = discord.Color.orange()
    elif percent < 75:
        color = discord.Color.gold()
    elif percent < 100:
        color = discord.Color.blue()
    else:
        color = discord.Color.green()

    filled = int((percent / 100) * 10)
    bar = "▰" * filled + "▱" * (10 - filled)
    progress_header = f"`[{bar}]` **{percent}%** — {step_title}"

    task_preview = task[:150] + "..." if len(task) > 150 else task

    embed = discord.Embed(
        title="⚡ Agentic AI Engine Active",
        description=(
            f"**Task:** {task_preview}\n"
            f"**Project:** `{repo}` (`main`)  •  **Brain:** `{model_label}`\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{progress_header}\n\n"
            f"### 📋 Live Execution Trace:"
        ),
        color=color
    )

    steps_text = "\n".join(steps_history) if steps_history else "Starting agent..."
    embed.add_field(name="Workflow Steps", value=steps_text, inline=False)

    if current_thought:
        embed.add_field(name="🧠 Agent Reasoning", value=f"*{current_thought[:300]}*", inline=False)

    embed.set_footer(text=f"Step {step_num} of 4 • Discord AI Commander Live Telemetry")
    return embed

def generate_diff_embed(repo, work_dir):
    try:
        res = subprocess.run(["git", "diff", "--stat"], cwd=work_dir, capture_output=True, text=True)
        stat_output = res.stdout.strip()
        diff_res = subprocess.run(["git", "diff"], cwd=work_dir, capture_output=True, text=True)
        raw_diff = diff_res.stdout.strip()

        if not raw_diff:
            return discord.Embed(
                title=f"📄 Code Diff • {repo}",
                description="ℹ️ No uncommitted changes detected in workspace.",
                color=discord.Color.light_gray()
            )

        if len(raw_diff) > 2600:
            display_diff = raw_diff[:2500] + "\n\n... [Remaining diff truncated for mobile view]"
        else:
            display_diff = raw_diff

        embed = discord.Embed(
            title=f"📄 Code Diff Preview • {repo}",
            description=f"**Files Changed Summary:**\n```\n{stat_output[:500]}\n```\n**Diff:**\n```diff\n{display_diff}\n```",
            color=discord.Color.teal()
        )
        embed.set_footer(text="Review the changes above • Tap Push to GitHub to deploy live!")
        return embed
    except Exception as e:
        return discord.Embed(title="❌ Diff Error", description=str(e), color=discord.Color.red())

# --- Discord Interactive Views ---

class StudioRepoSelect(discord.ui.Select):
    def __init__(self, repos):
        options = []
        for r in repos[:25]:
            options.append(discord.SelectOption(
                label=r[:100],
                value=r,
                description=f"Work on {r}",
                emoji="📁"
            ))
        super().__init__(placeholder="📂 Choose a repository to work on...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        session = get_session(interaction.user.id)
        selected_repo = self.values[0]
        session["active_repo"] = selected_repo
        session["active_branch"] = "main"

        # Advance to Step 2: Choose Model
        embed = discord.Embed(
            title="🧠 Step 2 of 3: Choose AI Brain",
            description=(
                f"Selected Project: **`{selected_repo}`** (`main`)\n\n"
                f"Choose the AI engine you want to use for writing & fixing code:"
            ),
            color=discord.Color.blue()
        )
        embed.set_footer(text="Step 2 of 3: Select an AI Model from the menu below")
        view = StudioModelView(session.get("active_model", "gemini-3.6-flash"))
        await interaction.response.edit_message(embed=embed, view=view)

class StudioRepoView(discord.ui.View):
    def __init__(self, repos):
        super().__init__(timeout=180)
        self.add_item(StudioRepoSelect(repos))

class StudioModelSelect(discord.ui.Select):
    def __init__(self, current_model):
        options = []
        for model_id, label in ALL_MODELS.items():
            options.append(discord.SelectOption(
                label=label[:100],
                value=model_id,
                default=(model_id == current_model),
                description=f"Use {model_id}"
            ))
        super().__init__(placeholder="🧠 Select an AI model...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        session = get_session(interaction.user.id)
        selected_model = self.values[0]
        session["active_model"] = selected_model
        repo = session.get("active_repo", "your repository")

        # Advance to Step 3: Prompt for Task with Control Buttons
        embed = discord.Embed(
            title="🎯 Step 3 of 3: Dev Studio Ready!",
            description=(
                f"**Active Repository:** `{repo}` (`main`)\n"
                f"**AI Engine:** `{selected_model}`\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"### 💬 What would you like to fix or build?\n\n"
                f"Type your task in chat using the format:\n"
                f"👉 **`fix: <describe your task>`**\n\n"
                f"*(Works in any case: `fix:`, `FIX:`, `Fix:`)*\n\n"
                f"**Examples:**\n"
                f"• `fix: change navbar background to navy blue and add a login button`\n"
                f"• `FIX: solve the audio pause issue on mobile`\n"
                f"• `Fix: add an API health check endpoint`"
            ),
            color=discord.Color.purple()
        )
        embed.set_footer(text="Type 'fix: <task>' in chat now to start coding!")
        view = StudioControlView(interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)

class StudioModelView(discord.ui.View):
    def __init__(self, current_model):
        super().__init__(timeout=180)
        self.add_item(StudioModelSelect(current_model))

class StudioControlView(discord.ui.View):
    """Control buttons for active workspace (Switch Model, Switch Repo, Restart, Close)."""
    def __init__(self, user_id):
        super().__init__(timeout=600)
        self.user_id = user_id

    @discord.ui.button(label="Switch Model", style=discord.ButtonStyle.blurple, emoji="🧠")
    async def switch_model_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return
        session = get_session(self.user_id)
        view = StudioModelView(session.get("active_model", "gemini-3.6-flash"))
        embed = discord.Embed(
            title="🧠 Switch AI Model",
            description="Select which model you want to switch to:",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Switch Repo", style=discord.ButtonStyle.gray, emoji="📁")
    async def switch_repo_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return
        repos = await asyncio.to_thread(fetch_user_repos)
        view = StudioRepoView(repos)
        embed = discord.Embed(
            title="📁 Switch Repository",
            description="Select which repository to switch to:",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Restart Chat", style=discord.ButtonStyle.gray, emoji="🔄")
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return
        session = get_session(self.user_id)
        if session.get("work_dir") and os.path.exists(session["work_dir"]):
            subprocess.run(["rm", "-rf", session["work_dir"]])
        session["active_repo"] = None
        session["pending_summary"] = None
        session["pending_files"] = []
        await interaction.response.send_message("🔄 Restarting studio...", ephemeral=True)
        await start_studio_flow(interaction.channel, self.user_id)

    @discord.ui.button(label="Close Chat", style=discord.ButtonStyle.red, emoji="❌")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return
        session = get_session(self.user_id)
        if session.get("work_dir") and os.path.exists(session["work_dir"]):
            subprocess.run(["rm", "-rf", session["work_dir"]])
        session["active_repo"] = None
        session["pending_summary"] = None
        session["pending_files"] = []
        await interaction.response.send_message("👋 Chat session closed! Type `lets code` anytime to start again.", ephemeral=False)

class PushActionView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=600)
        self.user_id = user_id

    @discord.ui.button(label="🚀 Push to GitHub & Deploy Live", style=discord.ButtonStyle.green, row=0)
    async def push_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return

        session = get_session(self.user_id)
        work_dir = session.get("work_dir")
        repo = session.get("active_repo")
        branch = session.get("active_branch", "main")
        summary = session.get("pending_summary", "Updated code via Discord Commander")

        if not work_dir or not os.path.exists(work_dir) or not session.get("pending_files"):
            await interaction.response.send_message("❌ No active changes found to push.", ephemeral=True)
            return

        await interaction.response.send_message("⏳ Committing and pushing to GitHub...", ephemeral=False)

        def _do_git_push():
            subprocess.run(["git", "config", "user.name", "Aditya (Discord Commander)"], cwd=work_dir, check=True)
            subprocess.run(["git", "config", "user.email", "connect.aditya002@gmail.com"], cwd=work_dir, check=True)
            subprocess.run(["git", "add", "-A"], cwd=work_dir, check=True)
            commit_msg = f"feat(discord-ai): {summary}\n\nAutomated via Discord Commander"
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=work_dir, check=True)
            push_res = subprocess.run(["git", "push", "origin", branch], cwd=work_dir, capture_output=True, text=True)
            if push_res.returncode != 0:
                return False, push_res.stderr, None
            sha_res = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=work_dir, capture_output=True, text=True)
            return True, None, sha_res.stdout.strip()

        try:
            ok, err_msg, commit_sha = await asyncio.to_thread(_do_git_push)
            if not ok:
                await interaction.followup.send(f"❌ **Push failed:**\n```\n{err_msg}\n```")
                return

            commit_url = f"https://github.com/{GH_USERNAME}/{repo}/commit/{commit_sha}"
            embed = discord.Embed(
                title="🚀 Changes Live & Deployed!",
                description=f"**{summary}**",
                color=discord.Color.green()
            )
            embed.add_field(name="Repository", value=f"[{repo}](https://github.com/{GH_USERNAME}/{repo})", inline=True)
            embed.add_field(name="Branch", value=f"`{branch}`", inline=True)
            embed.add_field(name="Commit", value=f"[`{commit_sha}`]({commit_url})", inline=True)
            embed.set_footer(text="✨ Render has detected this commit and is deploying live!")
            await interaction.followup.send(embed=embed)

            session["pending_summary"] = None
            session["pending_files"] = []
        except Exception as e:
            await interaction.followup.send(f"❌ Error during push: {e}")

    @discord.ui.button(label="View Code Diff", style=discord.ButtonStyle.secondary, emoji="📄", row=0)
    async def diff_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return
        session = get_session(self.user_id)
        work_dir = session.get("work_dir")
        repo = session.get("active_repo")
        if not work_dir or not os.path.exists(work_dir):
            await interaction.response.send_message("❌ No active workspace found.", ephemeral=True)
            return
        diff_embed = await asyncio.to_thread(generate_diff_embed, repo, work_dir)
        await interaction.response.send_message(embed=diff_embed, ephemeral=True)

    @discord.ui.button(label="Chat / Discuss", style=discord.ButtonStyle.blurple, emoji="💬", row=1)
    async def chat_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return
        await interaction.response.send_message(
            "💬 **Chat Mode Active!**\n"
            "Ask me anything about these changes, or decide what to do next. Type directly in chat!",
            ephemeral=False
        )

    @discord.ui.button(label="Switch Model", style=discord.ButtonStyle.gray, emoji="🧠", row=1)
    async def switch_model_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return
        session = get_session(self.user_id)
        view = StudioModelView(session.get("active_model", "gemini-3.5-flash-lite"))
        embed = discord.Embed(
            title="🧠 Switch AI Model",
            description="Select which model you want to use:",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Close / Discard", style=discord.ButtonStyle.red, emoji="❌", row=1)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return
        session = get_session(self.user_id)
        if session.get("work_dir") and os.path.exists(session["work_dir"]):
            subprocess.run(["rm", "-rf", session["work_dir"]])
        session["pending_summary"] = None
        session["pending_files"] = []
        await interaction.response.send_message("🗑️ Changes discarded & session closed. Type `lets code` to start fresh.", ephemeral=False)

class ApplyTaskModal(discord.ui.Modal, title="🚀 Execute Agentic Fix"):
    task_input = discord.ui.TextInput(
        label="What should the AI fix/build?",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. fix: update songs in Backend/data/kishoreSongs.json",
        max_length=500,
        required=True
    )

    def __init__(self, user_id, initial_value=""):
        super().__init__()
        self.user_id = user_id
        if initial_value:
            self.task_input.default = initial_value[:500]

    async def on_submit(self, interaction: discord.Interaction):
        task_text = self.task_input.value.strip()
        if not task_text:
            await interaction.response.send_message("⚠️ Task cannot be empty.", ephemeral=True)
            return
        await interaction.response.send_message(f"🚀 **Executing Decision:** `{task_text}`", ephemeral=False)
        ctx = await bot.get_context(interaction.message)
        for p in ["fix:", "fix :", "fix "]:
            if task_text.lower().startswith(p):
                task_text = task_text[len(p):].strip()
                break
        await code_cmd(ctx, task=task_text)

class ChatDecisionView(discord.ui.View):
    def __init__(self, user_id, repo, suggested_task=None, has_changes=False):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.repo = repo
        self.suggested_task = suggested_task
        self.has_changes = has_changes

    @discord.ui.button(label="Apply Decision", style=discord.ButtonStyle.green, emoji="🚀", row=0)
    async def apply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return
        session = get_session(self.user_id)
        default_task = self.suggested_task or session.get("last_suggested_task") or ""
        modal = ApplyTaskModal(self.user_id, initial_value=default_task)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Browse Files", style=discord.ButtonStyle.secondary, emoji="📁", row=0)
    async def browse_files_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return
        session = get_session(self.user_id)
        work_dir = session.get("work_dir")
        if not work_dir or not os.path.exists(work_dir):
            if self.repo:
                work_dir = await asyncio.to_thread(ensure_cloned_repo, self.user_id, self.repo, session.get("active_branch", "main"))
                session["work_dir"] = work_dir
            else:
                await interaction.response.send_message("⚠️ No active repository selected. Type `lets code` to pick one.", ephemeral=True)
                return
        _, all_paths = gather_repo_files(work_dir, max_files=100)
        lines = [f"📂 **Project Files for `{self.repo}`:**\n"]
        for p in all_paths[:35]:
            lines.append(f"• `{p}`")
        if len(all_paths) > 35:
            lines.append(f"\n*(and {len(all_paths) - 35} more files)*")
        embed = discord.Embed(title=f"📁 Repository Files • {self.repo}", description="\n".join(lines), color=discord.Color.blue())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="View Code Diff", style=discord.ButtonStyle.secondary, emoji="📄", row=0)
    async def diff_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return
        session = get_session(self.user_id)
        work_dir = session.get("work_dir")
        if not work_dir or not os.path.exists(work_dir):
            await interaction.response.send_message("❌ No active workspace found.", ephemeral=True)
            return
        diff_embed = await asyncio.to_thread(generate_diff_embed, self.repo, work_dir)
        await interaction.response.send_message(embed=diff_embed, ephemeral=True)

    @discord.ui.button(label="Switch Model", style=discord.ButtonStyle.blurple, emoji="🧠", row=1)
    async def switch_model_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return
        session = get_session(self.user_id)
        view = StudioModelView(session.get("active_model", "gemini-3.5-flash-lite"))
        embed = discord.Embed(
            title="🧠 Switch AI Model",
            description="Select which model you want to use for coding & chat:",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Restart Session", style=discord.ButtonStyle.gray, emoji="🔄", row=1)
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return
        session = get_session(self.user_id)
        if session.get("work_dir") and os.path.exists(session["work_dir"]):
            subprocess.run(["rm", "-rf", session["work_dir"]])
        session["active_repo"] = None
        session["pending_summary"] = None
        session["pending_files"] = []
        session["chat_history"] = []
        session["last_suggested_task"] = None
        await interaction.response.send_message("🔄 Restarting studio...", ephemeral=True)
        await start_studio_flow(interaction.channel, self.user_id)

# --- Studio Initiation Helper ---
async def start_studio_flow(destination, user_id):
    """Entrypoint when user says 'lets code'."""
    repos = fetch_user_repos()
    if not repos:
        await destination.send("⚠️ Could not fetch repositories from GitHub. Please check your GitHub token.")
        return

    embed = discord.Embed(
        title="🛠️ Agentic Dev Studio",
        description=(
            f"**Welcome Aditya!** Let's code on your projects.\n\n"
            f"👉 **Step 1:** Select a repository from the dropdown menu below:"
        ),
        color=discord.Color.brand_green()
    )
    embed.set_footer(text="Step 1 of 3: Choose Repository")
    view = StudioRepoView(repos)
    await destination.send(embed=embed, view=view)

# --- Bot Commands ---
@bot.event
async def on_ready():
    print(f"🤖 Discord AI Commander is READY as {bot.user} (ID: {bot.user.id})!", flush=True)
    print(f"🔗 Connected to {len(bot.guilds)} guilds, authorized username: '{DISCORD_USERNAME}'", flush=True)

@bot.command(name="help")
async def help_cmd(ctx):
    if not is_authorized(ctx.author):
        return
    embed = discord.Embed(title="📱 Discord AI Commander — Commands", color=discord.Color.blurple())
    embed.add_field(name="lets code", value="Start the interactive 3-step coding studio (Repo ➔ Model ➔ Task)", inline=False)
    embed.add_field(name="fix: <task>", value="Instruct the AI to read your repo and implement code edits", inline=False)
    embed.add_field(name="push", value="Commit & push your code to GitHub (triggers Render live auto-deploy)", inline=False)
    embed.add_field(name="switch model / model", value="Open menu to change model (Gemini 3.8 Flash, 3.5 Flash Lite, 3.1 Pro, 4 Argon, Claude Sonnet 4.6, Claude Opus 4.6, etc.)", inline=False)
    embed.add_field(name="restart / restart chat", value="Restart the studio session from Step 1", inline=False)
    embed.add_field(name="close / close chat", value="Close current session and clean workspace", inline=False)
    embed.add_field(name="repos", value="List all your 26+ GitHub repositories", inline=False)
    embed.add_field(name="repo <name>", value="Switch active repository directly", inline=False)
    embed.add_field(name="status", value="Show active repository, branch, and model", inline=False)
    embed.set_footer(text="All commands work in ANY case: upper, lower, or mixed!")
    await ctx.send(embed=embed)

@bot.command(name="status")
async def status_cmd(ctx):
    if not is_authorized(ctx.author):
        return
    session = get_session(ctx.author.id)
    embed = discord.Embed(title="⚙️ Current Dev Studio Status", color=discord.Color.blue())
    embed.add_field(name="Active Repository", value=f"`{session.get('active_repo') or 'None (type lets code)'}`", inline=True)
    embed.add_field(name="Branch", value=f"`{session.get('active_branch')}`", inline=True)
    embed.add_field(name="Active Model", value=f"`{session.get('active_model')}`", inline=False)
    embed.add_field(name="Pending Changes", value=f"{len(session.get('pending_files', []))} files modified", inline=True)
    embed.set_footer(text="Type 'lets code' to start or 'switch model' to change model")
    await ctx.send(embed=embed)

@bot.command(name="repos")
async def repos_cmd(ctx):
    if not is_authorized(ctx.author):
        return
    await ctx.send("🔍 Fetching your repositories from GitHub...")
    repo_list = fetch_user_repos()
    if not repo_list:
        await ctx.send("⚠️ No repositories found or GitHub token error.")
        return
    
    msg_lines = [f"**All Repositories for `{GH_USERNAME}`:**\n"]
    for i, r in enumerate(repo_list[:30], 1):
        msg_lines.append(f"`{i:2d}.` **{r}**")
    msg_lines.append("\n👉 *Type `lets code` to start, or `repo <name>` to select one!*")
    await ctx.send("\n".join(msg_lines))

@bot.command(name="repo")
async def set_repo_cmd(ctx, repo_name: str = None, branch: str = "main"):
    if not is_authorized(ctx.author):
        return
    if not repo_name:
        await ctx.send("⚠️ Please specify a repository name. Example: `repo RetroRaag` or type `lets code`")
        return

    session = get_session(ctx.author.id)
    session["active_repo"] = repo_name
    session["active_branch"] = branch
    await ctx.send(
        f"✅ **Active repository set to:** `{repo_name}` on branch `{branch}`!\n"
        f"👉 Now tell me what to fix/build: `fix: <your task description>`"
    )

@bot.command(name="model")
async def model_cmd(ctx, model_name: str = None):
    if not is_authorized(ctx.author):
        return
    session = get_session(ctx.author.id)

    if model_name:
        model_name_clean = model_name.strip().lower()
        matched = None
        for k in ALL_MODELS.keys():
            if model_name_clean == k.lower():
                matched = k
                break
        if matched:
            session["active_model"] = matched
            await ctx.send(f"✅ **Active Model updated to:** `{matched}`")
        else:
            avail = ", ".join([f"`{k}`" for k in ALL_MODELS.keys()])
            await ctx.send(f"⚠️ Unknown model. Available models:\n{avail}")
        return

    # Show interactive dropdown
    view = StudioModelView(session.get("active_model", "gemini-3.6-flash"))
    embed = discord.Embed(
        title="🧠 Choose AI Reasoning Engine",
        description="Select which model you want to write and fix your code:",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=view)

@bot.command(name="code")
async def code_cmd(ctx, *, task: str):
    if not is_authorized(ctx.author):
        return
    session = get_session(ctx.author.id)
    repo = session.get("active_repo")
    branch = session.get("active_branch", "main")
    model_name = session.get("active_model", "gemini-3.5-flash-lite")
    model_label = ALL_MODELS.get(model_name, model_name)

    if not repo:
        await ctx.send(
            "⚠️ No active repository selected!\n"
            "👉 Type **`lets code`** to pick a repository from the menu."
        )
        return

    # Workspace directory
    base_dir = os.path.expanduser("~/.gemini/antigravity-ide/scratch/discord_workspaces")
    os.makedirs(base_dir, exist_ok=True)
    work_dir = os.path.join(base_dir, f"{ctx.author.id}_{repo}")
    session["work_dir"] = work_dir

    if GH_PAT:
        clone_url = f"https://{GH_USERNAME}:{GH_PAT}@github.com/{GH_USERNAME}/{repo}.git"
    else:
        clone_url = f"https://github.com/{GH_USERNAME}/{repo}.git"

    # Step 1: Initialize & Clone
    step_history = ["🔄 Step 1: Initializing git workspace & checking credentials..."]
    progress_embed = create_agent_progress_embed(
        repo=repo,
        model_label=model_label,
        task=task,
        step_num=1,
        step_title="Cloning & Setting Up Workspace",
        percent=15,
        steps_history=step_history,
        current_thought=f"Setting up clean isolated local workspace for {repo} ({branch})..."
    )
    status_msg = await ctx.send(embed=progress_embed)

    def _prepare_and_clone():
        subprocess.run(["rm", "-rf", work_dir])
        res = subprocess.run(["git", "clone", clone_url, work_dir], capture_output=True, text=True)
        if res.returncode != 0:
            return False, res.stderr
        subprocess.run(["git", "checkout", branch], cwd=work_dir, capture_output=True)
        return True, None

    start_clone = time.time()
    clone_ok, clone_err = await asyncio.to_thread(_prepare_and_clone)
    clone_duration = time.time() - start_clone

    if not clone_ok:
        await status_msg.edit(content=f"❌ Failed to clone `{repo}`:\n```{clone_err}```", embed=None)
        return

    # Step 2: Indexing Files & Extracting External Context
    step_history = [
        f"✅ Step 1: Cloned `{repo}` (`{branch}`) in {clone_duration:.1f}s",
        "🔄 Step 2: Indexing source files & extracting external playlist/doc references..."
    ]
    progress_embed = create_agent_progress_embed(
        repo=repo,
        model_label=model_label,
        task=task,
        step_num=2,
        step_title="Indexing Files & Resolving Links",
        percent=35,
        steps_history=step_history,
        current_thought="Checking for Spotify playlists, documentation URLs, and scanning code..."
    )
    await status_msg.edit(embed=progress_embed)

    start_gather = time.time()
    enriched_task = await asyncio.to_thread(enrich_task_with_external_links, task)
    file_map, all_repo_paths = await asyncio.to_thread(gather_repo_files, work_dir, task)
    gather_duration = time.time() - start_gather

    spotify_note = ""
    if "[EXTRACTED SPOTIFY PLAYLIST TRACKS" in enriched_task:
        cnt = len(re.findall(r"^- ", enriched_task, re.MULTILINE))
        spotify_note = f" (Extracted {cnt} Spotify tracks)"

    file_list = "\n".join([f"- {p}" for p in all_repo_paths])

    # Step 3: Neural Reasoning & Code Generation
    step_history = [
        f"✅ Step 1: Cloned `{repo}` (`{branch}`) in {clone_duration:.1f}s",
        f"✅ Step 2: Indexed {len(file_map)} priority files{spotify_note} in {gather_duration:.1f}s",
        f"🔄 Step 3: Neural reasoning & synthesizing patch with {model_label}..."
    ]
    progress_embed = create_agent_progress_embed(
        repo=repo,
        model_label=model_label,
        task=task,
        step_num=3,
        step_title="Agentic Reasoning & Patch Synthesis",
        percent=65,
        steps_history=step_history,
        current_thought=f"Analyzing codebase and generating precision patch with {model_name}..."
    )
    await status_msg.edit(embed=progress_embed)

    system_instruction = (
        "You are an expert autonomous software engineer. "
        "Analyze the project carefully and make all required code edits to fulfill the task perfectly. "
        "Ensure syntax is valid and complete. Return ONLY a JSON object:\n"
        "{\n"
        '  "summary": "Brief 1-2 sentence description of what was changed",\n'
        '  "files": [\n'
        '    {\n'
        '      "path": "relative/path/to/file.ext",\n'
        '      "content": "Complete updated file content"\n'
        '    }\n'
        '  ]\n'
        "}\n"
    )

    user_prompt = f"Repository: {repo}\nTask: {enriched_task}\n\nFiles in repo:\n{file_list}\n\nFile Contents:\n"
    for p, c in file_map.items():
        user_prompt += f"\n--- FILE: {p} ---\n{c}\n"

    start_ai = time.time()
    try:
        ai_response, used_model = await asyncio.to_thread(call_ai, user_prompt, system_instruction, model_name)
    except Exception as e:
        await status_msg.edit(content=f"❌ **AI Error:** {e}", embed=None)
        return
    ai_duration = time.time() - start_ai

    summary = ai_response.get("summary", "Code updated via Discord Commander")
    changed_files = ai_response.get("files", [])

    if not changed_files:
        await status_msg.edit(content=f"⚠️ AI did not suggest any changes.\n**Note:** {summary}", embed=None)
        return

    # Step 4: Applying Patches to Disk
    step_history = [
        f"✅ Step 1: Cloned `{repo}` (`{branch}`) in {clone_duration:.1f}s",
        f"✅ Step 2: Indexed {len(file_map)} files{spotify_note}",
        f"✅ Step 3: {used_model} synthesized patch in {ai_duration:.1f}s",
        f"🔄 Step 4: Applying {len(changed_files)} file modification(s) to workspace..."
    ]
    progress_embed = create_agent_progress_embed(
        repo=repo,
        model_label=model_label,
        task=task,
        step_num=4,
        step_title="Applying Patches & Validating Diff",
        percent=90,
        steps_history=step_history,
        current_thought="Writing modified files to workspace and preparing git diff..."
    )
    await status_msg.edit(embed=progress_embed)

    for item in changed_files:
        target_path = os.path.join(work_dir, item["path"])
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as fp:
            fp.write(item["content"])

    session["pending_summary"] = summary
    session["pending_files"] = changed_files

    # Step 5: Final Finished Dashboard
    embed = discord.Embed(
        title="🚀 Agentic Patch Complete & Ready!",
        description=f"### {summary}\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        color=discord.Color.green()
    )
    embed.add_field(name="Target Project", value=f"`{repo}` (`{branch}`)", inline=True)
    embed.add_field(name="AI Engine Used", value=f"`{used_model}`", inline=True)
    file_bullets = "\n".join([f"• `{f['path']}`" for f in changed_files[:10]])
    embed.add_field(name=f"Modified Files ({len(changed_files)})", value=file_bullets, inline=False)
    embed.add_field(
        name="Next Action on Mobile",
        value="👉 Tap **[🚀 Push to GitHub & Deploy Live]** to publish to Render.\n👉 Tap **[📄 View Code Diff]** to preview changes on your phone.",
        inline=False
    )
    embed.set_footer(text="Aditya AI Dev • Code review ready • All changes staged")

    view = PushActionView(ctx.author.id)
    await status_msg.edit(content=None, embed=embed, view=view)

@bot.command(name="diff")
async def diff_cmd(ctx):
    if not is_authorized(ctx.author):
        return
    session = get_session(ctx.author.id)
    repo = session.get("active_repo")
    work_dir = session.get("work_dir")
    if not repo or not work_dir or not os.path.exists(work_dir):
        await ctx.send("⚠️ No active workspace or pending changes found. Select a repo and run a task first!")
        return
    diff_embed = await asyncio.to_thread(generate_diff_embed, repo, work_dir)
    await ctx.send(embed=diff_embed)

@bot.command(name="files")
async def files_cmd(ctx):
    if not is_authorized(ctx.author):
        return
    session = get_session(ctx.author.id)
    repo = session.get("active_repo")
    work_dir = session.get("work_dir")
    if not repo:
        await ctx.send("⚠️ No active repository selected. Type `lets code` to pick one!")
        return
    if not work_dir or not os.path.exists(work_dir):
        work_dir = await asyncio.to_thread(ensure_cloned_repo, ctx.author.id, repo, session.get("active_branch", "main"))
        session["work_dir"] = work_dir
    _, all_paths = gather_repo_files(work_dir, max_files=100)
    lines = [f"📂 **Project Files for `{repo}`:**\n"]
    for p in all_paths[:35]:
        lines.append(f"• `{p}`")
    if len(all_paths) > 35:
        lines.append(f"\n*(and {len(all_paths) - 35} more files)*")
    embed = discord.Embed(title=f"📁 Repository Files • {repo}", description="\n".join(lines), color=discord.Color.blue())
    await ctx.send(embed=embed)

async def chat_with_agent(message, ctx, user_text):
    session = get_session(message.author.id)
    repo = session.get("active_repo")
    model_name = session.get("active_model", "gemini-3.5-flash-lite")
    model_label = ALL_MODELS.get(model_name, model_name)

    async with message.channel.typing():
        repo_context = ""
        if repo:
            work_dir = await asyncio.to_thread(ensure_cloned_repo, message.author.id, repo, session.get("active_branch", "main"))
            session["work_dir"] = work_dir
            file_map, all_paths = await asyncio.to_thread(gather_repo_files, work_dir, user_text, 15, 30_000)
            file_tree_snippet = "\n".join([f"- {p}" for p in all_paths[:40]])
            files_content = "\n".join([f"--- FILE: {p} ---\n{c[:2500]}" for p, c in file_map.items()])
            repo_context = (
                f"\n\nRepository: {repo} (branch: {session.get('active_branch', 'main')})\n"
                f"Repository File List:\n{file_tree_snippet}\n\n"
                f"Relevant File Contents:\n{files_content}\n"
            )

        system_instruction = (
            f"You are Aditya AI Dev, a world-class autonomous AI pair programmer and software architect. "
            f"You are conversing with Aditya directly on his Discord mobile phone. "
            f"Your active AI model engine is: {model_name}.\n"
            "Guidelines:\n"
            "1. Answer questions clearly, accurately, and authoritatively. Format your response with clean Markdown, bullet points, and concise code blocks optimized for mobile screens.\n"
            "2. When discussing project files or architecture, cite the exact file paths.\n"
            "3. Help Aditya make decisions by weighing options, recommending best practices, and proposing next steps.\n"
            "4. At the very end of your response, include an actionable recommendation in this exact format:\n"
            "RECOMMENDED_ACTION: <a concise 1-sentence fix task that can be executed directly if approved, or NONE if no code change needed>\n"
        )

        full_prompt = f"{user_text}{repo_context}"
        try:
            ai_text, used_model = await asyncio.to_thread(call_ai_text, full_prompt, system_instruction, model_name, session["chat_history"])
        except Exception as e:
            await message.channel.send(f"❌ **AI Chat Error:** `{e}`")
            return

        rec_action = None
        if "RECOMMENDED_ACTION:" in ai_text:
            parts = ai_text.split("RECOMMENDED_ACTION:")
            clean_text = parts[0].strip()
            rec_str = parts[1].strip().split("\n")[0].strip()
            if rec_str.upper() != "NONE" and len(rec_str) > 3:
                rec_action = rec_str
        else:
            clean_text = ai_text.strip()

        session["last_suggested_task"] = rec_action

        session["chat_history"].append({"role": "user", "text": user_text})
        session["chat_history"].append({"role": "model", "text": clean_text})
        if len(session["chat_history"]) > 10:
            session["chat_history"] = session["chat_history"][-10:]

        embed = discord.Embed(
            title=f"💬 Agentic Dev Chat • {repo or 'Aditya Studio'}",
            description=clean_text[:4000],
            color=discord.Color.purple()
        )
        embed.set_author(name=f"Brain: {model_label[:55]}")

        if rec_action:
            embed.add_field(
                name="🎯 Recommended Decision",
                value=f"👉 **`fix: {rec_action}`**\n*(Tap **Apply Decision** below or reply `apply this`)*",
                inline=False
            )

        embed.set_footer(text="💬 Ask follow-up questions or use buttons below to decide")
        view = ChatDecisionView(message.author.id, repo, suggested_task=rec_action, has_changes=bool(session.get("pending_files")))
        await message.channel.send(embed=embed, view=view)

@bot.command(name="push")
async def push_cmd(ctx, *, commit_message: str = None):
    if not is_authorized(ctx.author):
        return
    session = get_session(ctx.author.id)
    work_dir = session.get("work_dir")
    repo = session.get("active_repo")
    branch = session.get("active_branch", "main")
    summary = session.get("pending_summary", "Updated code via Discord Commander")

    if not work_dir or not session.get("pending_files"):
        await ctx.send("⚠️ No pending changes to push. Use `fix: <task>` first!")
        return

    msg = await ctx.send("⏳ Committing and pushing to GitHub...")

    def _do_git_push():
        subprocess.run(["git", "config", "user.name", "Aditya (Discord Commander)"], cwd=work_dir, check=True)
        subprocess.run(["git", "config", "user.email", "connect.aditya002@gmail.com"], cwd=work_dir, check=True)
        subprocess.run(["git", "add", "-A"], cwd=work_dir, check=True)
        final_message = commit_message or summary
        commit_msg = f"feat(discord-ai): {final_message}\n\nAutomated via Discord Commander"
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=work_dir, check=True)
        push_res = subprocess.run(["git", "push", "origin", branch], cwd=work_dir, capture_output=True, text=True)
        if push_res.returncode != 0:
            return False, push_res.stderr, None
        sha_res = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=work_dir, capture_output=True, text=True)
        return True, None, sha_res.stdout.strip()

    ok, err_msg, commit_sha = await asyncio.to_thread(_do_git_push)
    if not ok:
        await msg.edit(content=f"❌ **Push failed:**\n```{err_msg}```")
        return
    commit_url = f"https://github.com/{GH_USERNAME}/{repo}/commit/{commit_sha}"

    embed = discord.Embed(
        title="🚀 Changes Live & Deployed!",
        description=f"**{summary}**",
        color=discord.Color.green()
    )
    embed.add_field(name="Repository", value=f"[{repo}](https://github.com/{GH_USERNAME}/{repo})", inline=True)
    embed.add_field(name="Branch", value=f"`{branch}`", inline=True)
    embed.add_field(name="Commit", value=f"[`{commit_sha}`]({commit_url})", inline=True)
    embed.set_footer(text="✨ Render has detected this commit and is deploying live!")
    await msg.edit(content=None, embed=embed)

    session["pending_summary"] = None
    session["pending_files"] = []

@bot.command(name="clear")
async def clear_chat_cmd(ctx):
    if not is_authorized(ctx.author):
        return
    channel = ctx.channel
    session = get_session(ctx.author.id)
    if session.get("work_dir") and os.path.exists(session["work_dir"]):
        subprocess.run(["rm", "-rf", session["work_dir"]])
    session["active_repo"] = None
    session["pending_summary"] = None
    session["pending_files"] = []

    # If in a guild text channel (where bulk purge is allowed)
    if isinstance(channel, discord.TextChannel):
        try:
            deleted = await channel.purge(limit=100)
            confirm_msg = await channel.send(f"🧹 Cleared {len(deleted)} messages from chat! Type `lets code` to start fresh.")
            await asyncio.sleep(4)
            try:
                await confirm_msg.delete()
            except Exception:
                pass
            return
        except discord.Forbidden:
            pass

    # If in a DM channel (Discord forbids deleting other users' messages in DMs, but allows deleting bot's messages)
    deleted_count = 0
    try:
        async for m in channel.history(limit=100):
            if m.author == bot.user:
                try:
                    await m.delete()
                    deleted_count += 1
                except Exception:
                    pass
    except Exception as e:
        print(f"Error purging messages in DM: {e}", flush=True)

    await channel.send(f"🧹 Chat cleared ({deleted_count} bot messages removed) & session reset!\n👉 Type **`lets code`** to start fresh.")

@bot.command(name="reset")
async def reset_cmd(ctx):
    if not is_authorized(ctx.author):
        return
    session = get_session(ctx.author.id)
    if session.get("work_dir") and os.path.exists(session["work_dir"]):
        subprocess.run(["rm", "-rf", session["work_dir"]])
    user_sessions[ctx.author.id] = {
        "active_repo": None,
        "active_branch": "main",
        "active_model": "gemini-3.5-flash-lite",
        "pending_summary": None,
        "pending_files": [],
        "work_dir": None,
        "chat_history": [],
        "last_suggested_task": None
    }
    await ctx.send("🧹 Session closed & reset. Type **`lets code`** anytime to start fresh!")

# --- Natural Language Message Handler (ANY CASE: Lower, Upper, Mixed!) ---
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    print(f"📩 [{message.author}] (ID: {message.author.id}) in {message.channel}: '{message.content}'", flush=True)

    try:
        if not is_authorized(message.author):
            print(f"🚫 Blocked unauthorized user: {message.author}", flush=True)
            await message.channel.send(
                f"🔒 **Access Restricted:** You are currently logged in as **{message.author}** (ID: `{message.author.id}`).\n"
                f"This bot is currently locked to `{DISCORD_USERNAME}`. Update `DISCORD_USERNAME` or `DISCORD_USER_ID` in your `.env` to authorize yourself."
            )
            return

        ctx = await bot.get_context(message)

        # If it starts with '!', process as standard command
        if message.content.startswith("!"):
            await bot.process_commands(message)
            return

        raw_content = message.content.strip()
        content_lower = raw_content.lower()

        # 1. Clear Chat (Works in ANY case: CLEAR, Clear, clear chat, CLEAR CHAT, cls, CLS, clean)
        if content_lower in ["clear", "clear chat", "clearchat", "cls", "clean", "clean chat"]:
            await clear_chat_cmd(ctx)
            return

        # 2. Close Chat / Stop / Exit (Works in ANY case: CLOSE, Close Chat, EXIT, QUIT, STOP)
        if content_lower in ["close", "close chat", "exit", "quit", "stop", "end chat", "bye", "cancel"]:
            await reset_cmd(ctx)
            return

        # 3. Restart Chat / Studio (Works in ANY case: RESTART, Restart Chat, REBOOT, etc.)
        if content_lower in ["restart", "restart chat", "re-start", "restart session", "reset chat", "reboot"]:
            session = get_session(message.author.id)
            if session.get("work_dir") and os.path.exists(session["work_dir"]):
                subprocess.run(["rm", "-rf", session["work_dir"]])
            session["active_repo"] = None
            session["pending_summary"] = None
            session["pending_files"] = []
            session["chat_history"] = []
            session["last_suggested_task"] = None
            await message.channel.send("🔄 Restarting studio session...")
            await start_studio_flow(message.channel, message.author.id)
            return

        # 4. Switch Model (Works in ANY case: SWITCH MODEL, Switch Model, CHANGE MODEL, MODEL, MODELS)
        if content_lower in ["switch model", "change model", "model", "models", "select model", "choose model"]:
            await model_cmd(ctx)
            return

        # 5. Switch Repo (Works in ANY case: SWITCH REPO, Change Repo, CHOOSE REPO)
        if content_lower in ["switch repo", "change repo", "choose repo", "select repo"]:
            await start_studio_flow(message.channel, message.author.id)
            return

        # 6. "lets code" studio trigger (Works in ANY case: LETS CODE, Lets Code, let's code, lets ccode, etc.)
        if content_lower in ["lets code", "let's code", "lets_code", "lets ccode", "let's ccode", "letscode", "let code", "start coding", "start code", "code", "start"]:
            await start_studio_flow(message.channel, message.author.id)
            return

        # 7. "fix: <task>" handler (Works in ANY case: FIX:, Fix:, fIx:, fix <task>)
        is_fix = False
        task_desc = ""
        for prefix in ["fix:", "fix :", "fix "]:
            if content_lower.startswith(prefix):
                is_fix = True
                task_desc = raw_content[len(prefix):].strip()
                break

        if is_fix:
            if not task_desc:
                await message.channel.send("⚠️ Please describe what you want to fix! Example: `fix: change hero button color to dark blue`")
                return
            await code_cmd(ctx, task=task_desc)
            return

        # 8. "apply this" / "do it" decision trigger
        if content_lower in ["apply this", "apply", "do this", "do it", "implement this", "go ahead", "apply solution", "execute"]:
            session = get_session(message.author.id)
            if session.get("last_suggested_task"):
                task = session["last_suggested_task"]
                await message.channel.send(f"🚀 **Executing decided action:** `{task}`")
                await code_cmd(ctx, task=task)
                return
            elif session.get("pending_files"):
                await push_cmd(ctx)
                return
            else:
                await message.channel.send("💡 No pending recommendation to apply yet. Ask a question or use `fix: <task>`!")
                return

        # 9. "diff" / "view diff" trigger
        if content_lower in ["diff", "view diff", "show diff", "git diff", "changes"]:
            await diff_cmd(ctx)
            return

        # 10. "files" / "show files" trigger
        if content_lower in ["files", "show files", "list files", "tree", "repo files", "project files"]:
            await files_cmd(ctx)
            return

        # 11. "push and deploy" handler (Works in ANY case: PUSH, DEPLOY, push and deploy, etc.)
        if content_lower in ["push", "deploy", "push and deploy", "push & deploy", "push code", "deploy code", "publish"]:
            await push_cmd(ctx)
            return
        elif content_lower.startswith("push ") or content_lower.startswith("deploy "):
            custom_comment = None
            for marker in ["with comment ", "with message ", "comment: ", "message: ", "with "]:
                if marker in content_lower:
                    idx = content_lower.find(marker) + len(marker)
                    custom_comment = raw_content[idx:].strip().strip('"').strip("'")
                    break
            await push_cmd(ctx, commit_message=custom_comment)
            return

        # 12. Other natural shortcuts (ANY CASE)
        if content_lower in ["help", "commands"]:
            await help_cmd(ctx)
        elif content_lower in ["repos", "show repos", "list repos", "my repos", "all repos"]:
            await repos_cmd(ctx)
        elif content_lower in ["status", "info", "session"]:
            await status_cmd(ctx)
        elif content_lower == "repo":
            await ctx.send("⚠️ Please specify a repository name! Example: `repo RetroRaag` or type `lets code`")
        elif content_lower.startswith("repo "):
            parts = raw_content.split()
            repo_name = parts[1] if len(parts) > 1 else None
            branch = parts[2] if len(parts) > 2 else "main"
            await set_repo_cmd(ctx, repo_name=repo_name, branch=branch)
        elif content_lower.startswith("use repo ") or content_lower.startswith("select repo "):
            parts = raw_content.split()
            repo_name = parts[2] if len(parts) > 2 else None
            branch = parts[3] if len(parts) > 3 else "main"
            await set_repo_cmd(ctx, repo_name=repo_name, branch=branch)
        else:
            # 13. Interactive Conversational Chat & Decision Making
            await chat_with_agent(message, ctx, raw_content)

    except Exception as e:
        print(f"❌ Error handling message: {e}", flush=True)
        import traceback
        traceback.print_exc()
        try:
            await message.channel.send(f"⚠️ Error processing command: `{e}`")
        except Exception:
            pass

# --- Health Check Server for Render 24/7 Hosting ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        is_ready = bot.is_ready()
        is_closed = bot.is_closed()
        try:
            latency = round(bot.latency * 1000, 2)
        except Exception:
            latency = None
        payload = {
            "status": "healthy" if (is_ready and not is_closed) else "reconnecting",
            "service": "Discord AI Commander",
            "bot_user": str(bot.user) if is_ready else "connecting",
            "is_ready": is_ready,
            "is_closed": is_closed,
            "latency_ms": latency,
            "authorized_username": DISCORD_USERNAME,
            "guilds": [g.name for g in bot.guilds],
            "models_available": list(ALL_MODELS.keys()),
            "message": "Bot is online and ready 24/7!"
        }
        self.wfile.write(json.dumps(payload, indent=2).encode("utf-8"))

    def log_message(self, format, *args):
        # Silence noisy periodic ping logs
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"🌍 24/7 Health server listening on port {port} (Render compatible)", flush=True)
    server.serve_forever()

if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN is not set in environment!", flush=True)
        sys.exit(1)

    # Start health server in daemon thread for Render 24/7 compatibility
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    while True:
        try:
            bot.run(DISCORD_BOT_TOKEN)
            break
        except discord.HTTPException as e:
            if getattr(e, "status", None) == 429:
                print(f"⏳ Discord API 429 (Temporary Cloudflare Rate Limit). Backing off for 60s before retrying...", flush=True)
                time.sleep(60)
            else:
                print(f"⚠️ Discord HTTP error: {e}. Retrying in 15s...", flush=True)
                time.sleep(15)
            script_path = os.path.abspath(__file__)
            os.execv(sys.executable, [sys.executable, script_path])
        except Exception as e:
            print(f"⚠️ bot.run exited with: {e}. Retrying in 15s...", flush=True)
            time.sleep(15)
            script_path = os.path.abspath(__file__)
            os.execv(sys.executable, [sys.executable, script_path])
