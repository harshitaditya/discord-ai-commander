import os
import sys
import json
import subprocess
import urllib.request
import urllib.error
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
import discord
from discord.ext import commands

load_dotenv()

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID")  # Only allow this user
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GH_PAT = os.environ.get("GH_PAT")
GH_USERNAME = os.environ.get("GH_USERNAME", "harshitaditya")

# All models available with Google AI Pro plan
GOOGLE_AI_PRO_MODELS = {
    "gemini-3.8-flash": "⚡ Gemini 3.8 Flash (High - Super Fast & Smart) [DEFAULT]",
    "gemini-2.5-pro": "🧠 Gemini 2.5 Pro (Deep Reasoning & Architecture)",
    "gemini-2.5-flash": "🚀 Gemini 2.5 Flash (Flagship Speed)",
    "gemini-2.0-flash": "⚡ Gemini 2.0 Flash (Next-Gen Fast)",
    "gemini-1.5-pro": "📚 Gemini 1.5 Pro (Massive 2M Context Window)",
    "gemini-1.5-flash": "⚡ Gemini 1.5 Flash (Classic Fast)",
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

DISCORD_USERNAME = os.environ.get("DISCORD_USERNAME", "aditya_ak02")

def is_authorized(user):
    """Check if the user is authorized by username or user ID."""
    if not DISCORD_USER_ID and not DISCORD_USERNAME:
        return True
    user_str_id = str(getattr(user, "id", user))
    user_name = str(getattr(user, "name", user))
    if DISCORD_USER_ID and user_str_id == str(DISCORD_USER_ID):
        return True
    if DISCORD_USERNAME and user_name.lower() == DISCORD_USERNAME.lower():
        return True
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
        print(f"Error fetching repos: {e}")
        return []

def gather_repo_files(repo_dir, max_total_bytes=1_500_000):
    ignore_dirs = {".git", "node_modules", "dist", "build", ".next", ".vscode", "__pycache__", "target", "vendor", ".idea"}
    ignore_exts = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".zip", ".tar", ".gz", ".exe", ".bin", ".mp4", ".lock", ".pyc"}
    file_map = {}
    total_bytes = 0

    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in ignore_exts or f.endswith("-lock.json"):
                continue
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
            except Exception:
                pass
    return file_map

# --- Gemini API Call ---
def call_gemini(prompt, system_instruction, model_name="gemini-3.8-flash"):
    models_to_try = [model_name, "gemini-2.5-flash", "gemini-1.5-flash"]
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
            print(f"Error calling {m}: {e}")
            last_err = e

    raise RuntimeError(f"Gemini API error. Last error: {last_err}")

# --- Discord Interactive Views ---
class ModelSelect(discord.ui.Select):
    def __init__(self, current_model):
        options = []
        for model_id, label in GOOGLE_AI_PRO_MODELS.items():
            options.append(discord.SelectOption(
                label=label[:100],
                value=model_id,
                default=(model_id == current_model),
                description=f"Select {model_id}"
            ))
        super().__init__(placeholder="Choose a Google AI Pro model...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        session = get_session(interaction.user.id)
        selected = self.values[0]
        session["active_model"] = selected
        label = GOOGLE_AI_PRO_MODELS.get(selected, selected)
        await interaction.response.send_message(f"✅ **Active Model switched to:** `{selected}`\n*{label}*", ephemeral=True)

class ModelView(discord.ui.View):
    def __init__(self, current_model):
        super().__init__(timeout=60)
        self.add_item(ModelSelect(current_model))

class PushActionView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=300)
        self.user_id = user_id

    @discord.ui.button(label="🚀 Push & Go Live on Render", style=discord.ButtonStyle.green)
    async def push_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return

        session = get_session(self.user_id)
        work_dir = session.get("work_dir")
        repo = session.get("active_repo")
        branch = session.get("active_branch")
        summary = session.get("pending_summary", "Updated code via Discord Commander")

        if not work_dir or not os.path.exists(work_dir):
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

    @discord.ui.button(label="❌ Discard Changes", style=discord.ButtonStyle.red)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your session.", ephemeral=True)
            return
        session = get_session(self.user_id)
        if session.get("work_dir"):
            subprocess.run(["rm", "-rf", session["work_dir"]])
        session["pending_summary"] = None
        session["pending_files"] = []
        await interaction.response.send_message("🗑️ Changes discarded.", ephemeral=True)

# --- Render Web Service Health Endpoint ---
from aiohttp import web

async def handle_ping(request):
    return web.Response(text="Discord AI Commander is Online 24/7!")

async def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Render Health Server listening on port {port}", flush=True)

# --- Bot Commands ---
@bot.event
async def on_ready():
    print(f"🤖 Discord AI Commander is ready as {bot.user}!", flush=True)
    try:
        await start_health_server()
    except Exception as e:
        print(f"Health server info: {e}", flush=True)

@bot.command(name="help")
async def help_cmd(ctx):
    if not is_authorized(ctx.author):
        return
    embed = discord.Embed(title="📱 Discord AI Commander — Commands", color=discord.Color.blurple())
    embed.add_field(name="!repos", value="List all your GitHub repositories", inline=False)
    embed.add_field(name="!repo <name> [branch]", value="Select your active working repository (default branch: `main`)", inline=False)
    embed.add_field(name="!model", value="Open interactive menu with all Google AI Pro models", inline=False)
    embed.add_field(name="!status", value="Show current active repo, branch, and model", inline=False)
    embed.add_field(name="!code <task>", value="Instruct the AI to read your repo and make code changes", inline=False)
    embed.add_field(name="!push", value="Commit & push your changes to GitHub (triggers Render live deploy)", inline=False)
    embed.add_field(name="!reset", value="Clear active repository and pending workspace", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="status")
async def status_cmd(ctx):
    if not is_authorized(ctx.author):
        return
    session = get_session(ctx.author.id)
    embed = discord.Embed(title="⚙️ Current Session Status", color=discord.Color.blue())
    embed.add_field(name="Active Repository", value=f"`{session.get('active_repo') or 'None (use !repo <name>)'}`", inline=True)
    embed.add_field(name="Branch", value=f"`{session.get('active_branch')}`", inline=True)
    embed.add_field(name="Active Model", value=f"`{session.get('active_model')}`", inline=False)
    embed.add_field(name="Pending Changes", value=f"{len(session.get('pending_files', []))} files modified", inline=True)
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
    msg_lines.append("\n👉 *Type `!repo <name>` to choose one to work on!*")
    await ctx.send("\n".join(msg_lines))

@bot.command(name="repo")
async def set_repo_cmd(ctx, repo_name: str = None, branch: str = "main"):
    if not is_authorized(ctx.author):
        return
    if not repo_name:
        await ctx.send("⚠️ Please specify a repository name. Example: `!repo Billing-Sphere`")
        return

    session = get_session(ctx.author.id)
    session["active_repo"] = repo_name
    session["active_branch"] = branch
    await ctx.send(f"✅ **Active repository set to:** `{repo_name}` on branch `{branch}`!\n👉 Now tell me what you want to change: `!code <your task>`")

@bot.command(name="model")
async def model_cmd(ctx, model_name: str = None):
    if not is_authorized(ctx.author):
        return
    session = get_session(ctx.author.id)

    if model_name:
        if model_name in GOOGLE_AI_PRO_MODELS:
            session["active_model"] = model_name
            await ctx.send(f"✅ **Active Model updated to:** `{model_name}`")
        else:
            avail = ", ".join([f"`{k}`" for k in GOOGLE_AI_PRO_MODELS.keys()])
            await ctx.send(f"⚠️ Unknown model. Available models in your Google AI Pro plan:\n{avail}")
        return

    # Show interactive dropdown
    view = ModelView(session["active_model"])
    embed = discord.Embed(
        title="🧠 Select Your Google AI Pro Model",
        description=f"**Current active model:** `{session['active_model']}`\n\nChoose from the dropdown below:",
        color=discord.Color.gold()
    )
    for mid, desc in GOOGLE_AI_PRO_MODELS.items():
        embed.add_field(name=f"`{mid}`", value=desc, inline=False)
    await ctx.send(embed=embed, view=view)

@bot.command(name="code")
async def code_cmd(ctx, *, task: str = None):
    if not is_authorized(ctx.author):
        return
    session = get_session(ctx.author.id)
    repo = session.get("active_repo")
    branch = session.get("active_branch", "main")
    model_name = session.get("active_model", "gemini-3.8-flash")

    if not repo:
        await ctx.send("⚠️ Please set a repository first! Example:\n`!repo Billing-Sphere`")
        return
    if not task:
        await ctx.send("⚠️ Please describe what you want changed. Example:\n`!code Change the hero button to green and update header title`")
        return

    status_msg = await ctx.send(f"⏳ **Working on `{repo}` using `{model_name}`...**\n- Cloning repo from GitHub...")

    work_dir = os.path.abspath(f"discord_workspace_{ctx.author.id}_{repo}")
    session["work_dir"] = work_dir
    clone_url = f"https://x-access-token:{GH_PAT}@github.com/{GH_USERNAME}/{repo}.git"

    subprocess.run(["rm", "-rf", work_dir])
    res = subprocess.run(["git", "clone", clone_url, work_dir], capture_output=True, text=True)
    if res.returncode != 0:
        await status_msg.edit(content=f"❌ Failed to clone `{repo}`:\n```{res.stderr}```")
        return

    subprocess.run(["git", "checkout", branch], cwd=work_dir, capture_output=True)

    await status_msg.edit(content=f"⏳ **Reading project files and consulting `{model_name}`...**")
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
        ai_response, used_model = call_gemini(user_prompt, system_instruction, model_name)
    except Exception as e:
        await status_msg.edit(content=f"❌ **Gemini Error:** {e}")
        return

    summary = ai_response.get("summary", "Code updated via Discord Commander")
    changed_files = ai_response.get("files", [])

    if not changed_files:
        await status_msg.edit(content=f"⚠️ Gemini did not suggest any changes.\n**Note:** {summary}")
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
    embed.set_footer(text="Tap the button below to push to GitHub & deploy live on Render!")

    view = PushActionView(ctx.author.id)
    await status_msg.edit(content=None, embed=embed, view=view)

@bot.command(name="push")
async def push_cmd(ctx, *, commit_message: str = None):
    if not is_authorized(ctx.author):
        return
    session = get_session(ctx.author.id)
    work_dir = session.get("work_dir")
    repo = session.get("active_repo")
    branch = session.get("active_branch")
    summary = session.get("pending_summary", "Updated code via Discord Commander")

    if not work_dir or not session.get("pending_files"):
        await ctx.send("⚠️ No pending changes to push. Use `!code <task>` first!")
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
    embed.set_footer(text="✨ Render has detected this push and is deploying your site live!")
    await msg.edit(content=None, embed=embed)

    session["pending_summary"] = None
    session["pending_files"] = []

@bot.command(name="reset")
async def reset_cmd(ctx):
    if not is_authorized(ctx.author):
        return
    session = get_session(ctx.author.id)
    if session.get("work_dir"):
        subprocess.run(["rm", "-rf", session["work_dir"]])
    user_sessions[ctx.author.id] = {
        "active_repo": None,
        "active_branch": "main",
        "active_model": "gemini-3.8-flash",
        "pending_summary": None,
        "pending_files": [],
        "work_dir": None
    }
    await ctx.send("🧹 Session reset. Tell me which repo to work on: e.g. `repo Test`")

# --- Natural Language Message Handler (No '!' needed!) ---
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if not is_authorized(message.author):
        return

    ctx = await bot.get_context(message)

    # If it starts with '!', process as standard command
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    content_clean = message.content.strip().lower()
    raw_content = message.content.strip()

    # Natural shortcuts without '!'
    if content_clean in ["help", "commands"]:
        await help_cmd(ctx)
    elif content_clean in ["repos", "show repos", "list repos", "my repos", "all repos"]:
        await repos_cmd(ctx)
    elif content_clean in ["model", "models", "select model", "change model"]:
        await model_cmd(ctx)
    elif content_clean in ["status", "info"]:
        await status_cmd(ctx)
    elif content_clean.startswith("push") or content_clean.startswith("deploy"):
        # Handles:
        # "push this code with comment my message"
        # "push with comment my message"
        # "push this code"
        # "push"
        custom_comment = None
        for marker in ["with comment ", "with message ", "comment: ", "message: "]:
            if marker in content_clean:
                idx = content_clean.find(marker) + len(marker)
                custom_comment = raw_content[idx:].strip().strip('"').strip("'")
                break
        await push_cmd(ctx, commit_message=custom_comment)
    elif content_clean in ["reset", "clear"]:
        await reset_cmd(ctx)
    elif content_clean.startswith("repo "):
        parts = raw_content.split()
        repo_name = parts[1] if len(parts) > 1 else None
        branch = parts[2] if len(parts) > 2 else "main"
        await set_repo_cmd(ctx, repo_name=repo_name, branch=branch)
    elif content_clean.startswith("use repo ") or content_clean.startswith("select repo "):
        parts = raw_content.split()
        repo_name = parts[2] if len(parts) > 2 else None
        branch = parts[3] if len(parts) > 3 else "main"
        await set_repo_cmd(ctx, repo_name=repo_name, branch=branch)
    else:
        # User typed plain English instructions!
        session = get_session(message.author.id)
        if session.get("active_repo"):
            # Repo is active, treat plain message as coding task!
            await code_cmd(ctx, task=raw_content)
        else:
            await ctx.send(
                "👋 Hello Aditya! What repository would you like to work on?\n"
                "• Type `repos` to see all your repositories.\n"
                "• Type `repo <name>` to select one (e.g. `repo Test`)."
            )

# --- Health Check Server for Render 24/7 Hosting ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        bot_user_str = str(bot.user) if bot.is_ready() else "connecting"
        payload = {
            "status": "healthy",
            "service": "Discord AI Commander",
            "bot_user": bot_user_str,
            "models_available": list(GOOGLE_AI_PRO_MODELS.keys()),
            "message": "Bot is online and ready 24/7!"
        }
        self.wfile.write(json.dumps(payload, indent=2).encode("utf-8"))

    def log_message(self, format, *args):
        # Silence noisy periodic ping logs
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    print(f"🌍 24/7 Health server listening on port {port} (Render compatible)")
    server.serve_forever()

if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN is not set in environment!")
        sys.exit(1)

    # Start health server in daemon thread for Render 24/7 compatibility
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    bot.run(DISCORD_BOT_TOKEN)
