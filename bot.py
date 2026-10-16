import os
import sys
import json
import subprocess
import urllib.request
import urllib.error
import threading
import time
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

# All supported AI Models
ALL_MODELS = {
    "gemini-3.8-flash": "⚡ Gemini 3.8 Flash (High — Super Fast & Smart) [DEFAULT]",
    "gemini-3.5-flash-lite": "⚡ Gemini 3.5 Flash Lite (Ultra Fast & Lightweight)",
    "gemini-3.1-pro": "🧠 Gemini 3.1 Pro (Deep Logic & Complex Architecture)",
    "gemini-4-argon": "🔮 Gemini 4 Argon (Next-Gen Autonomous Agentic Engine)",
    "claude-sonnet-4-6": "🎭 Claude Sonnet 4.6 (Elite Coding & Refactoring)",
    "claude-opus-4-6": "👑 Claude Opus 4.6 (Deep Architectural Reasoning)",
    "gemini-2.5-pro": "🧠 Gemini 2.5 Pro (Flagship Reasoning & Architecture)",
    "gemini-2.5-flash": "🚀 Gemini 2.5 Flash (Flagship Speed)",
    "gemini-2.0-flash": "⚡ Gemini 2.0 Flash (Next-Gen Fast)",
    "gemini-1.5-pro": "📚 Gemini 1.5 Pro (Massive 2M Context Window)",
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
            "active_model": "gemini-3.8-flash",
            "pending_summary": None,
            "pending_files": [],
            "work_dir": None
        }
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

def gather_repo_files(repo_dir, max_files=60, max_total_bytes=300_000):
    """Recursively collect source files to feed into AI prompt."""
    file_map = {}
    ignored_dirs = {".git", "node_modules", "vendor", "__pycache__", ".vscode", "dist", "build", ".next", ".cache", "venv", ".idea"}
    ignored_exts = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".zip", ".tar", ".gz", ".lock", ".woff", ".woff2", ".ttf", ".pdf", ".exe", ".bin"}

    total_bytes = 0
    count = 0

    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in ignored_exts:
                continue
            if count >= max_files:
                break
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, repo_dir)
            try:
                size = os.path.getsize(full_path)
                if size > 150_000:
                    continue
                with open(full_path, "r", encoding="utf-8", errors="ignore") as fp:
                    content = fp.read()
                if total_bytes + len(content) <= max_total_bytes:
                    file_map[rel_path] = content
                    total_bytes += len(content)
                    count += 1
            except Exception:
                pass
    return file_map

# --- AI Calling (Gemini & Claude) ---
def call_gemini(prompt, system_instruction, model_name="gemini-3.8-flash"):
    preferred = [model_name]
    fallbacks = ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-flash"]
    models_to_try = []
    for m in preferred + fallbacks:
        if m not in models_to_try:
            models_to_try.append(m)
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
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                text_response = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text_response), m
        except Exception as e:
            print(f"Error calling {m}: {e}", flush=True)
            last_err = e

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

def call_ai(prompt, system_instruction, model_name="gemini-3.8-flash"):
    """Unified AI caller supporting Gemini and Claude models."""
    if model_name in ["claude-sonnet-4-6", "claude-opus-4-6"]:
        if ANTHROPIC_API_KEY:
            target_claude = "claude-3-7-sonnet-20250219" if model_name == "claude-sonnet-4-6" else "claude-3-opus-20240229"
            try:
                return call_anthropic(prompt, system_instruction, target_claude)
            except Exception as e:
                print(f"Anthropic API error: {e}, falling back to Gemini Pro...", flush=True)

        persona = (
            f"You are {model_name}, an elite software engineer. "
            "Think deeply and generate clean, bug-free production-grade modifications. "
        )
        return call_gemini(prompt, persona + system_instruction, "gemini-2.5-pro")

    return call_gemini(prompt, system_instruction, model_name)

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
        view = StudioModelView(session.get("active_model", "gemini-3.8-flash"))
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
        view = StudioModelView(session.get("active_model", "gemini-3.8-flash"))
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
        repos = fetch_user_repos()
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

    @discord.ui.button(label="🚀 Push to GitHub & Deploy Live", style=discord.ButtonStyle.green)
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

        try:
            subprocess.run(["git", "config", "user.name", "Aditya (Discord Commander)"], cwd=work_dir, check=True)
            subprocess.run(["git", "config", "user.email", "connect.aditya002@gmail.com"], cwd=work_dir, check=True)
            subprocess.run(["git", "add", "-A"], cwd=work_dir, check=True)
            commit_msg = f"feat(discord-ai): {summary}\n\nAutomated via Discord Commander"
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=work_dir, check=True)
            push_res = subprocess.run(["git", "push", "origin", branch], cwd=work_dir, capture_output=True, text=True)

            if push_res.returncode != 0:
                await interaction.followup.send(f"❌ **Push failed:**\n```\n{push_res.stderr}\n```")
                return

            sha_res = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=work_dir, capture_output=True, text=True)
            commit_sha = sha_res.stdout.strip()
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

    @discord.ui.button(label="Switch Model", style=discord.ButtonStyle.blurple, emoji="🧠")
    async def switch_model_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return
        session = get_session(self.user_id)
        view = StudioModelView(session.get("active_model", "gemini-3.8-flash"))
        embed = discord.Embed(
            title="🧠 Switch AI Model",
            description="Select which model you want to use:",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Restart Chat", style=discord.ButtonStyle.gray, emoji="🔄")
    async def restart_button(self, interaction: discord.Interaction, button: discord.ui.Button):
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

    @discord.ui.button(label="Close / Discard", style=discord.ButtonStyle.red, emoji="❌")
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
    view = StudioModelView(session.get("active_model", "gemini-3.8-flash"))
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
    model_name = session.get("active_model", "gemini-3.8-flash")

    if not repo:
        await ctx.send(
            "⚠️ No active repository selected!\n"
            "👉 Type **`lets code`** to pick a repository from the menu."
        )
        return

    status_msg = await ctx.send(f"⏳ **Connecting to `{repo}` on GitHub...**")

    # Workspace directory
    base_dir = os.path.expanduser("~/.gemini/antigravity-ide/scratch/discord_workspaces")
    os.makedirs(base_dir, exist_ok=True)
    work_dir = os.path.join(base_dir, f"{ctx.author.id}_{repo}")
    session["work_dir"] = work_dir

    if GH_PAT:
        clone_url = f"https://{GH_USERNAME}:{GH_PAT}@github.com/{GH_USERNAME}/{repo}.git"
    else:
        clone_url = f"https://github.com/{GH_USERNAME}/{repo}.git"

    subprocess.run(["rm", "-rf", work_dir])
    res = subprocess.run(["git", "clone", clone_url, work_dir], capture_output=True, text=True)
    if res.returncode != 0:
        await status_msg.edit(content=f"❌ Failed to clone `{repo}`:\n```{res.stderr}```")
        return

    subprocess.run(["git", "checkout", branch], cwd=work_dir, capture_output=True)

    model_label = ALL_MODELS.get(model_name, model_name)
    await status_msg.edit(content=f"⏳ **Reading project files and consulting `{model_label}`...**")
    file_map = gather_repo_files(work_dir)
    file_list = "\n".join([f"- {p}" for p in file_map.keys()])

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

    user_prompt = f"Repository: {repo}\nTask: {task}\n\nFiles in repo:\n{file_list}\n\nFile Contents:\n"
    for p, c in file_map.items():
        user_prompt += f"\n--- FILE: {p} ---\n{c}\n"

    try:
        ai_response, used_model = call_ai(user_prompt, system_instruction, model_name)
    except Exception as e:
        await status_msg.edit(content=f"❌ **AI Error:** {e}")
        return

    summary = ai_response.get("summary", "Code updated via Discord Commander")
    changed_files = ai_response.get("files", [])

    if not changed_files:
        await status_msg.edit(content=f"⚠️ AI did not suggest any changes.\n**Note:** {summary}")
        return

    for item in changed_files:
        target_path = os.path.join(work_dir, item["path"])
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as fp:
            fp.write(item["content"])

    session["pending_summary"] = summary
    session["pending_files"] = changed_files

    embed = discord.Embed(
        title="✅ Code Changes Prepared!",
        description=f"**{summary}**",
        color=discord.Color.green()
    )
    embed.add_field(name="Target Repo", value=f"`{repo}` (`{branch}`)", inline=True)
    embed.add_field(name="Model Used", value=f"`{used_model}`", inline=True)
    file_bullets = "\n".join([f"• `{f['path']}`" for f in changed_files[:10]])
    embed.add_field(name="Modified Files", value=file_bullets, inline=False)
    embed.set_footer(text="Tap the button below or type 'push' to deploy live on Render!")

    view = PushActionView(ctx.author.id)
    await status_msg.edit(content=None, embed=embed, view=view)

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
    subprocess.run(["git", "config", "user.name", "Aditya (Discord Commander)"], cwd=work_dir, check=True)
    subprocess.run(["git", "config", "user.email", "connect.aditya002@gmail.com"], cwd=work_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=work_dir, check=True)
    final_message = commit_message or summary
    commit_msg = f"feat(discord-ai): {final_message}\n\nAutomated via Discord Commander"
    subprocess.run(["git", "commit", "-m", commit_msg], cwd=work_dir, check=True)
    push_res = subprocess.run(["git", "push", "origin", branch], cwd=work_dir, capture_output=True, text=True)

    if push_res.returncode != 0:
        await msg.edit(content=f"❌ **Push failed:**\n```{push_res.stderr}```")
        return

    sha_res = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=work_dir, capture_output=True, text=True)
    commit_sha = sha_res.stdout.strip()
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
        "active_model": "gemini-3.8-flash",
        "pending_summary": None,
        "pending_files": [],
        "work_dir": None
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

        # 1. Close Chat / Stop / Exit (Works in ANY case: CLOSE, Close Chat, EXIT, QUIT, STOP)
        if content_lower in ["close", "close chat", "exit", "quit", "stop", "end chat", "bye", "reset", "clear", "cancel"]:
            await reset_cmd(ctx)
            return

        # 2. Restart Chat / Studio (Works in ANY case: RESTART, Restart Chat, REBOOT, etc.)
        if content_lower in ["restart", "restart chat", "re-start", "restart session", "reset chat", "reboot"]:
            session = get_session(message.author.id)
            if session.get("work_dir") and os.path.exists(session["work_dir"]):
                subprocess.run(["rm", "-rf", session["work_dir"]])
            session["active_repo"] = None
            session["pending_summary"] = None
            session["pending_files"] = []
            await message.channel.send("🔄 Restarting studio session...")
            await start_studio_flow(message.channel, message.author.id)
            return

        # 3. Switch Model (Works in ANY case: SWITCH MODEL, Switch Model, CHANGE MODEL, MODEL, MODELS)
        if content_lower in ["switch model", "change model", "model", "models", "select model", "choose model"]:
            await model_cmd(ctx)
            return

        # 4. Switch Repo (Works in ANY case: SWITCH REPO, Change Repo, CHOOSE REPO)
        if content_lower in ["switch repo", "change repo", "choose repo", "select repo"]:
            await start_studio_flow(message.channel, message.author.id)
            return

        # 5. "lets code" studio trigger (Works in ANY case: LETS CODE, Lets Code, let's code, etc.)
        if content_lower in ["lets code", "let's code", "lets_code", "start coding", "start code", "code", "start"]:
            await start_studio_flow(message.channel, message.author.id)
            return

        # 6. "fix: <task>" handler (Works in ANY case: FIX:, Fix:, fIx:, fix <task>)
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

        # 7. "push and deploy" handler (Works in ANY case: PUSH, DEPLOY, push and deploy, etc.)
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

        # 8. Other natural shortcuts (ANY CASE)
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
            # Fallback natural message
            session = get_session(message.author.id)
            if session.get("active_repo"):
                await message.channel.send(
                    f"💡 Active Repo: `{session.get('active_repo')}` (`{session.get('active_branch')}`)\n"
                    f"👉 To edit code, type: **`fix: {raw_content}`**\n"
                    f"👉 To switch model, type: **`switch model`**\n"
                    f"👉 To restart session, type: **`restart`**\n"
                    f"👉 To close chat, type: **`close`**\n"
                    f"👉 To push changes, type: **`push`**"
                )
            else:
                await start_studio_flow(message.channel, message.author.id)

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
        except Exception as e:
            print(f"⚠️ bot.run exited with: {e}. Reconnecting in 5s...", flush=True)
            time.sleep(5)
