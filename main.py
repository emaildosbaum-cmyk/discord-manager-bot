import asyncio
import os
import sys
import json
import logging
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

# Configuração de Logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DiscordManager")

# Configuração do Token
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
def get_token():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if token:
        return token
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("bot_token")
        except Exception:
            pass
    return None

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.emojis = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =====================================================================
#  VERIFICAÇÃO DE SEGURANÇA E RESTRIÇÃO DE ACESSO
# =====================================================================
def is_authorized_admin(interaction: discord.Interaction) -> bool:
    """Verifica se o usuário é o Dono do Servidor ou possui permissão de Administrador."""
    if not interaction.guild:
        return False
    if interaction.user.id == interaction.guild.owner_id:
        return True
    if isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator:
        return True
    return False

async def check_admin_permission(interaction: discord.Interaction) -> bool:
    """Garante que membros comuns recebam bloqueio imediato."""
    if not is_authorized_admin(interaction):
        await interaction.response.send_message(
            "⛔ **Acesso Negado:** Apenas o Dono do Servidor ou Administradores autorizados podem executar comandos deste bot.",
            ephemeral=True
        )
        return False
    return True


# =====================================================================
#  BOTÕES DE CONFIRMAÇÃO COM SEGURANÇA (DISCORD UI)
# =====================================================================
class ConfirmDangerAction(discord.ui.View):
    def __init__(self, author_id: int, action_name: str):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.action_name = action_name
        self.value = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Apenas quem disparou este comando pode confirmar.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Sim, Confirmar e Executar", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self.stop()
        await interaction.response.edit_message(content="❌ Operação cancelada pelo usuário.", view=None)


# =====================================================================
#  FUNÇÕES AUXILIARES DE CLONAGEM
# =====================================================================
def build_overwrites(old_overwrites: dict, role_map: dict, target_guild: discord.Guild) -> dict:
    new_overwrites = {}
    for target, perms in old_overwrites.items():
        if isinstance(target, discord.Role):
            if target.is_default():
                new_overwrites[target_guild.default_role] = perms
            elif target in role_map:
                new_overwrites[role_map[target]] = perms
    return new_overwrites

async def execute_clone_roles(source_guild: discord.Guild, target_guild: discord.Guild) -> tuple[dict, int]:
    role_map = {source_guild.default_role: target_guild.default_role}
    try:
        await target_guild.default_role.edit(permissions=source_guild.default_role.permissions)
    except Exception:
        pass

    roles = [r for r in source_guild.roles if not r.is_default() and not r.managed]
    roles.sort(key=lambda r: r.position)
    existing_roles = {r.name: r for r in target_guild.roles if not r.managed}

    created_count = 0
    for role in roles:
        try:
            if role.name in existing_roles:
                role_map[role] = existing_roles[role.name]
                continue

            new_role = await target_guild.create_role(
                name=role.name,
                permissions=role.permissions,
                color=role.color,
                hoist=role.hoist,
                mentionable=role.mentionable,
                reason=f"Clonado de {source_guild.name}"
            )
            role_map[role] = new_role
            created_count += 1
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning(f"Erro ao criar cargo {role.name}: {e}")

    return role_map, created_count

async def execute_clone_channels(source_guild: discord.Guild, target_guild: discord.Guild, role_map: dict = None) -> tuple[int, int]:
    if role_map is None:
        role_map = {source_guild.default_role: target_guild.default_role}
        target_roles = {r.name: r for r in target_guild.roles}
        for r in source_guild.roles:
            if r.name in target_roles:
                role_map[r] = target_roles[r.name]

    cat_map = {}
    categories_created = 0
    channels_created = 0

    # Categorias
    for cat in sorted(source_guild.categories, key=lambda c: c.position):
        try:
            overwrites = build_overwrites(cat.overwrites, role_map, target_guild)
            new_cat = await target_guild.create_category(name=cat.name, overwrites=overwrites)
            cat_map[cat.id] = new_cat
            categories_created += 1
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning(f"Erro na categoria {cat.name}: {e}")

    # Canais de Texto
    for ch in sorted(source_guild.text_channels, key=lambda c: c.position):
        try:
            parent = cat_map.get(ch.category_id) if ch.category_id else None
            overwrites = build_overwrites(ch.overwrites, role_map, target_guild)
            await target_guild.create_text_channel(
                name=ch.name,
                category=parent,
                topic=ch.topic,
                slowmode_delay=ch.slowmode_delay,
                nsfw=ch.nsfw,
                overwrites=overwrites
            )
            channels_created += 1
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning(f"Erro no canal #{ch.name}: {e}")

    # Canais de Voz
    for vc in sorted(source_guild.voice_channels, key=lambda c: c.position):
        try:
            parent = cat_map.get(vc.category_id) if vc.category_id else None
            overwrites = build_overwrites(vc.overwrites, role_map, target_guild)
            bitrate = min(vc.bitrate, int(target_guild.bitrate_limit))
            await target_guild.create_voice_channel(
                name=vc.name,
                category=parent,
                bitrate=bitrate,
                user_limit=vc.user_limit,
                overwrites=overwrites
            )
            channels_created += 1
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning(f"Erro no canal de voz {vc.name}: {e}")

    return categories_created, channels_created

async def execute_clone_emojis(source_guild: discord.Guild, target_guild: discord.Guild) -> int:
    existing = {e.name for e in target_guild.emojis}
    copied = 0
    for emoji in source_guild.emojis:
        if emoji.name in existing:
            continue
        try:
            img = await emoji.read()
            await target_guild.create_custom_emoji(name=emoji.name, image=img)
            copied += 1
            await asyncio.sleep(0.5)
        except discord.HTTPException as e:
            if e.code == 30008:
                break
        except Exception:
            pass
    return copied


# =====================================================================
#  SLASH COMMANDS DO DISCORD
# =====================================================================

@bot.tree.command(name="setup", description="Painel de controle e status do ServerManager.")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_setup(interaction: discord.Interaction):
    if not await check_admin_permission(interaction):
        return

    guild = interaction.guild
    bot_member = guild.me
    bot_top_role = bot_member.top_role
    highest_server_role = guild.roles[-1]

    is_top_hierarchy = (bot_top_role.position >= highest_server_role.position)
    other_servers = [g.name for g in bot.guilds if g.id != guild.id]

    embed = discord.Embed(
        title="🛡️ ServerManager — Painel de Controle",
        description="Sistema de gerenciamento e clonagem de servidor ativo e protegido.",
        color=0x335FFF
    )
    embed.add_field(name="📍 Servidor Atual", value=f"**{guild.name}** (`{guild.id}`)", inline=False)
    embed.add_field(name="👑 Dono do Servidor", value=f"<@{guild.owner_id}>", inline=True)
    embed.add_field(name="⚡ Permissão Admin", value="✅ Concedida" if bot_member.guild_permissions.administrator else "❌ Ausente", inline=True)
    embed.add_field(name="📶 Posição no Topo", value="✅ No Topo dos Cargos" if is_top_hierarchy else "⚠️ Suba o cargo do bot para o topo!", inline=True)

    servers_desc = "\n".join([f"• `{g.name}` (`{g.id}`)" for g in bot.guilds])
    embed.add_field(name="🌐 Servidores Conectados ao Bot", value=servers_desc[:1024] if servers_desc else "Apenas este", inline=False)

    embed.add_field(
        name="📜 Comandos Disponíveis (Privados)",
        value=(
            "`/clonar_tudo [id_origem]` — Clona cargos, canais e emojis\n"
            "`/clonar_cargos [id_origem]` — Clona apenas os cargos\n"
            "`/clonar_canais [id_origem]` — Clona apenas categorias e canais\n"
            "`/clonar_emojis [id_origem]` — Clona os emojis personalizados\n"
            "`/apagar_categoria [categoria]` — Apaga uma categoria e todos os canais nela\n"
            "`/limpar_canais` — Reseta todos os canais do servidor\n"
            "`/limpar_cargos` — Reseta todos os cargos personalizados"
        ),
        inline=False
    )
    embed.set_footer(text="Segurança Ativa: Somente administradores têm acesso a estes comandos.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="clonar_tudo", description="Clona cargos, categorias, canais e emojis de outro servidor.")
@app_commands.describe(id_origem="ID do servidor de onde você quer copiar")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_clonar_tudo(interaction: discord.Interaction, id_origem: str):
    if not await check_admin_permission(interaction):
        return

    try:
        source_id = int(id_origem.strip())
        source_guild = bot.get_guild(source_id)
    except ValueError:
        return await interaction.response.send_message("❌ ID do servidor inválido.", ephemeral=True)

    if not source_guild:
        return await interaction.response.send_message("❌ Não encontrei esse servidor. Verifique se o bot está adicionado nele!", ephemeral=True)

    if source_guild.id == interaction.guild_id:
        return await interaction.response.send_message("❌ O servidor de origem não pode ser o mesmo atual.", ephemeral=True)

    target_guild = interaction.guild
    view = ConfirmDangerAction(interaction.user.id, "Clonar Tudo")

    await interaction.response.send_message(
        f"⚠️ **Confirmar Clonagem Total**\n"
        f"Copiar de: **{source_guild.name}**\n"
        f"Para este servidor: **{target_guild.name}**\n"
        f"Isso criará cargos, categorias, canais e emojis idênticos.",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if view.value:
        msg = await interaction.followup.send("🚀 [1/3] Iniciando clonagem de cargos...", ephemeral=True)
        role_map, roles_cnt = await execute_clone_roles(source_guild, target_guild)
        await msg.edit(content=f"✅ Cargos clonados ({roles_cnt}).\n🚀 [2/3] Clonando categorias e canais...")
        cats_cnt, chs_cnt = await execute_clone_channels(source_guild, target_guild, role_map)
        await msg.edit(content=f"✅ Cargos ({roles_cnt}) e Canais ({chs_cnt}) clonados.\n🚀 [3/3] Clonando emojis...")
        emojis_cnt = await execute_clone_emojis(source_guild, target_guild)
        await msg.edit(content=f"🎉 **Clonagem Completa Concluída com Sucesso!**\n• {roles_cnt} Cargos criados\n• {cats_cnt} Categorias criadas\n• {chs_cnt} Canais criados\n• {emojis_cnt} Emojis copiados")


@bot.tree.command(name="clonar_cargos", description="Clona APENAS os cargos de outro servidor.")
@app_commands.describe(id_origem="ID do servidor de origem")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_clonar_cargos(interaction: discord.Interaction, id_origem: str):
    if not await check_admin_permission(interaction):
        return

    try:
        source_id = int(id_origem.strip())
        source_guild = bot.get_guild(source_id)
    except ValueError:
        return await interaction.response.send_message("❌ ID do servidor inválido.", ephemeral=True)

    if not source_guild:
        return await interaction.response.send_message("❌ Servidor de origem não encontrado. O bot precisa estar nele!", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    _, roles_cnt = await execute_clone_roles(source_guild, interaction.guild)
    await interaction.followup.send(f"✅ **Sucesso:** {roles_cnt} cargos clonados de **{source_guild.name}** com cores e permissões!", ephemeral=True)


@bot.tree.command(name="clonar_canais", description="Clona APENAS as categorias e canais de outro servidor.")
@app_commands.describe(id_origem="ID do servidor de origem")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_clonar_canais(interaction: discord.Interaction, id_origem: str):
    if not await check_admin_permission(interaction):
        return

    try:
        source_id = int(id_origem.strip())
        source_guild = bot.get_guild(source_id)
    except ValueError:
        return await interaction.response.send_message("❌ ID do servidor inválido.", ephemeral=True)

    if not source_guild:
        return await interaction.response.send_message("❌ Servidor de origem não encontrado.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    cats_cnt, chs_cnt = await execute_clone_channels(source_guild, interaction.guild)
    await interaction.followup.send(f"✅ **Sucesso:** {cats_cnt} categorias e {chs_cnt} canais clonados de **{source_guild.name}**!", ephemeral=True)


@bot.tree.command(name="clonar_emojis", description="Clona os emojis de outro servidor.")
@app_commands.describe(id_origem="ID do servidor de origem")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_clonar_emojis(interaction: discord.Interaction, id_origem: str):
    if not await check_admin_permission(interaction):
        return

    try:
        source_id = int(id_origem.strip())
        source_guild = bot.get_guild(source_id)
    except ValueError:
        return await interaction.response.send_message("❌ ID inválido.", ephemeral=True)

    if not source_guild:
        return await interaction.response.send_message("❌ Servidor não encontrado.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    count = await execute_clone_emojis(source_guild, interaction.guild)
    await interaction.followup.send(f"✅ **Sucesso:** {count} emojis copiados de **{source_guild.name}**!", ephemeral=True)


@bot.tree.command(name="apagar_categoria", description="Apaga uma categoria inteira e todos os canais contidos nela.")
@app_commands.describe(categoria="Selecione a categoria para apagar com todos os seus canais")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_apagar_categoria(interaction: discord.Interaction, categoria: discord.CategoryChannel):
    if not await check_admin_permission(interaction):
        return

    channels_count = len(categoria.channels)
    view = ConfirmDangerAction(interaction.user.id, "Apagar Categoria")

    await interaction.response.send_message(
        f"⚠️ **ATENÇÃO:** Isso irá apagar permanentemente a categoria **📁 {categoria.name}** e os seus **{channels_count} canais**!\n"
        f"Confirma a exclusão em massa?",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if view.value:
        msg = await interaction.followup.send("🗑️ Apagando canais da categoria...", ephemeral=True)
        for ch in list(categoria.channels):
            try:
                await ch.delete(reason="Exclusão em massa solicitada via comando")
                await asyncio.sleep(0.3)
            except Exception:
                pass

        try:
            await categoria.delete(reason="Exclusão em massa de categoria")
        except Exception:
            pass

        await msg.edit(content=f"✅ Categoria **{categoria.name}** e todos os seus {channels_count} canais foram apagados com sucesso!")


@bot.tree.command(name="limpar_canais", description="🚨 Reseta o servidor: Apaga TODOS os canais existentes.")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_limpar_canais(interaction: discord.Interaction):
    if not await check_admin_permission(interaction):
        return

    guild = interaction.guild
    view = ConfirmDangerAction(interaction.user.id, "Limpar Todos os Canais")

    await interaction.response.send_message(
        f"🚨 **PERIGO MÁXIMO:** Você está prestes a apagar **TODOS OS {len(guild.channels)} CANAIS** de **{guild.name}**!\n"
        f"Esta ação não pode ser desfeita.",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if view.value:
        # Cria um canal de log temporário para o bot poder responder
        temp_ch = await guild.create_text_channel(name="suporte-reset", reason="Canal temporário durante reset")
        for ch in list(guild.channels):
            if ch.id == temp_ch.id:
                continue
            try:
                await ch.delete(reason="Reset geral de canais")
                await asyncio.sleep(0.3)
            except Exception:
                pass
        await temp_ch.send("✅ **Reset de canais concluído!** Apenas este canal foi mantido para você continuar.")


@bot.tree.command(name="limpar_cargos", description="🚨 Apaga todos os cargos personalizados do servidor.")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_limpar_cargos(interaction: discord.Interaction):
    if not await check_admin_permission(interaction):
        return

    guild = interaction.guild
    roles = [r for r in guild.roles if not r.is_default() and not r.managed and r < guild.me.top_role]
    view = ConfirmDangerAction(interaction.user.id, "Limpar Todos os Cargos")

    await interaction.response.send_message(
        f"⚠️ **Confirmar Reset de Cargos:** {len(roles)} cargos personalizados serão excluídos em **{guild.name}**.",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if view.value:
        deleted = 0
        for r in roles:
            try:
                await r.delete(reason="Reset de cargos solicitado por administrador")
                deleted += 1
                await asyncio.sleep(0.3)
            except Exception:
                pass
        await interaction.followup.send(f"✅ Reset concluído! {deleted} cargos foram excluídos.", ephemeral=True)


# =====================================================================
#  SERVIDOR WEB DE HEALTH CHECK (Para manter ativo no Render/Railway)
# =====================================================================
async def handle_healthcheck(request):
    status_data = {
        "status": "online",
        "bot": bot.user.name if bot.user else "connecting",
        "servers": len(bot.guilds),
        "latency_ms": round(bot.latency * 1000, 2)
    }
    return web.json_response(status_data)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_healthcheck)
    app.router.add_get("/health", handle_healthcheck)
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Servidor Web de Health Check ativo na porta {port}")


# =====================================================================
#  EVENTOS DO BOT
# =====================================================================
@bot.event
async def on_ready():
    logger.info(f"Bot conectado como {bot.user} (ID: {bot.user.id})")
    
    # 1. Atualiza o avatar do bot se houver 'avatar.jpg' na pasta
    avatar_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "avatar.jpg")
    if os.path.exists(avatar_file):
        try:
            with open(avatar_file, "rb") as f:
                avatar_bytes = f.read()
            await bot.user.edit(avatar=avatar_bytes)
            logger.info("Avatar do bot atualizado com sucesso!")
        except discord.HTTPException as e:
            logger.warning(f"Aviso de taxa de limite para avatar (Discord permite 2 por 10min): {e}")
        except Exception as e:
            logger.warning(f"Não foi possível atualizar avatar automaticamente: {e}")

    # 2. Sincroniza os comandos Slash globalmente
    try:
        synced = await bot.tree.sync()
        logger.info(f"Sincronizados {len(synced)} comandos Slash com sucesso!")
    except Exception as e:
        logger.error(f"Erro ao sincronizar comandos: {e}")

    # 3. Inicia o web server para o Render/Railway
    try:
        await start_web_server()
    except Exception as e:
        logger.warning(f"Aviso ao iniciar web server: {e}")

    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="seus servidores | /setup"),
        status=discord.Status.online
    )


def run():
    token = get_token()
    if not token:
        print("[X] Token não encontrado no ambiente nem no config.json.")
        sys.exit(1)
    bot.run(token)


if __name__ == "__main__":
    run()
