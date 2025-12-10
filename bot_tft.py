import discord
from discord.ext import commands
import aiohttp
import json
import os
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

# CONFIG (change ici)
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
RIOT_API_KEY = os.getenv("RIOT_API_KEY")
REGION = 'euw1'
DATA_FILE = '/data/players.json'

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Valeurs pour trier les tiers (score = tier_value * 100 + LP)
TIER_VALUES = {
    'UNRANKED': 0, 'IRON': 100, 'BRONZE': 200, 'SILVER': 300, 'GOLD': 400,
    'PLATINUM': 500, 'EMERALD': 600, 'DIAMOND': 700, 'MASTER': 800,
    'GRANDMASTER': 900, 'CHALLENGER': 1000
}

def load_players():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f).get('players', [])
    return []

def save_players(players):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump({'players': players}, f, ensure_ascii=False, indent=2)

async def get_uuid(session, name, tag):
    url = f'https://europe.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{name}/{tag}'
    async with session.get(url, params={'api_key': RIOT_API_KEY}) as resp:
        if resp.status == 200:
            data = await resp.json()
            return data.get('puuid')
    return None

async def get_league(session, uuid):
    url = f'https://{REGION}.api.riotgames.com/tft/league/v1/by-puuid/{uuid}'
    async with session.get(url, params={'api_key': RIOT_API_KEY}) as resp:
        if resp.status == 200:
            data = await resp.json()
            for entry in data:
                if entry['queueType'] == 'RANKED_TFT':
                    return entry
    return None

async def get_match_ids(session, uuid, count=5):
    url = f"https://europe.api.riotgames.com/tft/match/v1/matches/by-puuid/{uuid}/ids"
    async with session.get(url, params={"api_key": RIOT_API_KEY, "count": count}) as resp:
        if resp.status == 200:
            return await resp.json()
    return []

async def get_match_data(session, match_id):
    url = f"https://europe.api.riotgames.com/tft/match/v1/matches/{match_id}"
    async with session.get(url, params={"api_key": RIOT_API_KEY}) as resp:
        if resp.status == 200:
            return await resp.json()
    return None

@bot.event
async def on_ready():
    print(f'{bot.user} connecté ! Utilise !add <pseudo> pour commencer.')

@bot.command()
async def add(ctx, *, nameAndTag: str):
    name = nameAndTag.split('#')[0].strip();
    tag = nameAndTag.split('#')[1].strip();
    
    players = load_players()
    if any(p['name'].lower() == name.lower() for p in players):
        await ctx.send(f"❌ **{name}** est déjà dans le classement.")
        return

    async with aiohttp.ClientSession() as session:
        uuid = await get_uuid(session, name, tag)
        if not uuid:
            await ctx.send(f"❌ **{name}** non trouvé sur {REGION.upper()}. Vérifie le pseudo/région.")
            return

    players.append({'name': name, 'uuid': uuid})
    save_players(players)
    await ctx.send(f"✅ **{name}** ajouté au classement !")

@bot.command(aliases=['supp', 'del'])
async def remove(ctx, *, name: str):
    players = load_players()
    old_len = len(players)
    players = [p for p in players if p['name'].lower() != name.lower()]
    if len(players) == old_len:
        await ctx.send(f"❌ **{name}** n'est pas dans le classement.")
        return
    save_players(players)
    await ctx.send(f"✅ **{name}** retiré du classement.")

@bot.command()
async def removeAll(ctx, *, name: str):
    players = []
    save_players(players)
    await ctx.send(f"💀 Le classement a été totalement supprimé.")

@bot.command(aliases=['lb', 'rank'])
async def classement(ctx):
    players = load_players()
    if not players:
        await ctx.send("❌ Aucun joueur dans le classement. Utilise `!add <pseudo>`.")
        return

    player_stats = []
    async with aiohttp.ClientSession() as session:
        for p in players:
            league = await get_league(session, p['uuid'])
            player_stats.append((p['name'], league))

    # Stats valides (ranked TFT)
    valid_stats = [(name, league) for name, league in player_stats if league]
    if not valid_stats:
        await ctx.send("❌ Aucun joueur ranké dans le classement.")
        return

    # Tri par score
    def get_score(league):
        tier = league['tier']
        lp = league['leaguePoints']
        return TIER_VALUES.get(tier, 0) * 100 + lp

    valid_stats.sort(key=lambda x: get_score(x[1]), reverse=True)

    # Embed
    embed = discord.Embed(title="🏆 Classement TFT (Live)", color=0x00ff00, timestamp=ctx.message.created_at)
    desc = ""
    for i, (name, league) in enumerate(valid_stats[:10], 1):
        tier = league['tier']
        rank_div = league['rank']
        lp = league['leaguePoints']
        wins = league['wins']
        losses = league['losses']
        games = wins + losses
        wr = round((wins / games * 100), 1) if games else 0
        desc += f"{i}. **{name}** | {tier} {rank_div} **({lp} LP)** | {wr}% ({games} games)\n"

    embed.description = desc

    # Non rankés
    unranked = [name for name, league in player_stats if not league]
    if unranked:
        embed.add_field(name="⚪ Non rankés", value=" | ".join(unranked), inline=False)

    embed.set_footer(text=f"Région: {REGION.upper()} | {len(valid_stats)} rankés")
    await ctx.send(embed=embed)

@bot.command()
async def liste(ctx):
    players = load_players()
    if not players:
        await ctx.send("Aucun joueur.")
        return
    names = [p['name'] for p in players]
    await ctx.send(f"👥 Joueurs suivis ({len(names)}): {' | '.join(names)}")

@bot.command()
async def stats(ctx, *, name: str):
    players = load_players()

    # Vérifier si le joueur est dans la liste
    player = next((p for p in players if p['name'].lower() == name.lower()), None)
    if not player:
        await ctx.send(f"❌ **{name}** n'est pas dans la liste. Ajoute-le avec `!add {name}#TAG`.")
        return

    async with aiohttp.ClientSession() as session:
        league = await get_league(session, player['uuid'])

    if not league:
        await ctx.send(f"⚪ **{name}** n'a **pas de classement TFT**.")
        return

    # ---- Extraction des stats ----
    tier = league['tier']
    rank_div = league['rank']
    lp = league['leaguePoints']
    wins = league['wins']
    losses = league['losses']
    games = wins + losses
    wr = round((wins / games * 100), 1) if games else 0

    # Embed stylé
    embed = discord.Embed(
        title=f"📊 Statistiques TFT — {name}",
        description=f"Statistiques actuelles sur **{REGION.upper()}**",
        color=0x3498db
    )

    embed.add_field(
        name="🏆 Rang",
        value=f"**{tier} {rank_div}** ({lp} LP)",
        inline=False
    )

    embed.add_field(
        name="📈 Winrate",
        value=f"**{wr}%** sur {games} games",
        inline=True
    )

    embed.add_field(
        name="🔵 Victoires",
        value=f"**{wins}**",
        inline=True
    )

    embed.add_field(
        name="🔴 Défaites",
        value=f"**{losses}**",
        inline=True
    )

    # Image d'icône de tier (optionnel si tu veux)
    embed.set_thumbnail(url=f"https://static.bigbrain.gg/assets/tft/tiers/{tier.lower()}.png")

    embed.set_footer(text="Données issues de l'API Riot Games")

    await ctx.send(embed=embed)
    
@bot.command()
async def compare(ctx, *, args: str):
    player1, player2 = args.split(" vs ")
    players = load_players()

    # Récupérer les joueurs
    p1 = next((p for p in players if p['name'].lower() == player1.lower()), None)
    p2 = next((p for p in players if p['name'].lower() == player2.lower()), None)

    if not p1:
        await ctx.send(f"❌ Le joueur **{player1}** n'est pas dans la liste.")
        return
    if not p2:
        await ctx.send(f"❌ Le joueur **{player2}** n'est pas dans la liste.")
        return

    async with aiohttp.ClientSession() as session:
        l1 = await get_league(session, p1['uuid'])
        l2 = await get_league(session, p2['uuid'])

    if not l1 or not l2:
        await ctx.send("❌ Les deux joueurs doivent être **classés** pour une comparaison.")
        return

    # Statistiques
    def extract(league):
        tier = league['tier']
        div = league['rank']
        lp = league['leaguePoints']
        wins = league['wins']
        losses = league['losses']
        games = wins + losses
        wr = round((wins / games * 100), 1) if games else 0
        return tier, div, lp, wins, losses, games, wr

    t1, d1, lp1, w1, lo1, g1, wr1 = extract(l1)
    t2, d2, lp2, w2, lo2, g2, wr2 = extract(l2)

    # Embed comparaison
    embed = discord.Embed(
        title=f"⚔️ Comparaison TFT — {player1} vs {player2}",
        color=0xe67e22
    )

    embed.add_field(
        name=f"🟦 {player1}",
        value=f"**{t1} {d1}** ({lp1} LP)\nWR: **{wr1}%**\nGames: {g1}",
        inline=True
    )

    embed.add_field(
        name=f"🟥 {player2}",
        value=f"**{t2} {d2}** ({lp2} LP)\nWR: **{wr2}%**\nGames: {g2}",
        inline=True
    )

    # Verdict
    winner = player1 if lp1 > lp2 else player2
    embed.add_field(
        name="🏆 Avantage",
        value=f"Avantage actuel : **{winner}**",
        inline=False
    )

    await ctx.send(embed=embed)
    
@bot.command()
async def history(ctx, *, name: str):
    players = load_players()
    player = next((p for p in players if p['name'].lower() == name.lower()), None)

    if not player:
        await ctx.send(f"❌ **{name}** n'est pas dans la liste.")
        return

    async with aiohttp.ClientSession() as session:
        # Récupérer les 5 derniers match IDs
        match_ids = await get_match_ids(session, player['uuid'], 5)

        if not match_ids:
            await ctx.send("❌ Impossible de récupérer l'historique.")
            return

        matches = []
        for match_id in match_ids:
            data = await get_match_data(session, match_id)
            if not data:
                continue
            # Chercher le participant correspondant
            for p in data["info"]["participants"]:
                if p["puuid"] == player["uuid"]:
                    matches.append(p)
                    break

    # Embed historique
    embed = discord.Embed(
        title=f"📜 Historique récent — {name}",
        color=0x9b59b6
    )

    for i, m in enumerate(matches, 1):
        placement = m["placement"]
        queue = m.get("tft_game_type", "Ranked/Normal")
        time = m["time_eliminated"]

        embed.add_field(
            name=f"Partie #{i} — Top **{placement}**",
            value=f"Mode : `{queue}`\nTemps élimination : {round(time/60)} min",
            inline=False
        )

    embed.set_footer(text="Top 1 = incroyable. Top 8 = dommage 😭")

    await ctx.send(embed=embed)

@bot.command(aliases=["ranked_History"])
async def ranked(ctx, *, args: str):
    # ---------- Parser le pseudo + nombre de parties ----------
    default_count = 5
    count = default_count
    name = args.strip()

    # on sépare pseudo et nombre de games
    parts = args.rsplit(" ", 1)

    if len(parts) == 2:
        raw_count = parts[1].replace(",", ".")  # gère x.x et x,x
        try:
            value = float(raw_count)
            count = int(value)

            # Bornes
            if count < 1:
                count = 1
            if count > 10:
                count = 10

            name = parts[0].strip()
        except ValueError:
            # si ce n'est pas un nombre, on garde tout comme pseudo
            name = args.strip()

    name = name.strip()

    if not name:
        await ctx.send("❌ Tu dois préciser un pseudo. Exemple : `!ranked Γαχιss 3`")
        return

    players = load_players()
    player = next((p for p in players if p['name'].lower() == name.lower()), None)

    if not player:
        await ctx.send(f"❌ **{name}** n'est pas dans la liste.")
        return

    async with aiohttp.ClientSession() as session:

        # On récupère plus de games brutes car filtrage (normal / ranked)
        raw_limit = count * 4
        if raw_limit < 20:
            raw_limit = 20
        if raw_limit > 80:
            raw_limit = 80

        match_ids = await get_match_ids(session, player['uuid'], raw_limit)

        if not match_ids:
            await ctx.send("❌ Impossible de récupérer l'historique.")
            return

        matches = []
        for match_id in match_ids:
            data = await get_match_data(session, match_id)
            if not data:
                continue

            info = data.get("info", {})

            # On garde que les ranked : queue_id = 1100
            if info.get("queue_id") != 1100:
                continue

            # Chercher le participant correspondant
            for p in info.get("participants", []):
                if p["puuid"] == player["uuid"]:
                    matches.append(p)
                    break

            # On s'arrête dès qu'on a le nombre de games demandé
            if len(matches) >= count:
                break

    if not matches:
        await ctx.send(f"⚪ **{name}** n'a pas de parties classées récentes.")
        return

    # ---------- Emoji par placement ----------
    PLACEMENT_EMOJIS = {
        1: "🥇",
        2: "🥈",
        3: "🥉",
        4: "🙂",
        5: "🙃",
        6: "😥",
        7: "😢",
        8: "😭",
    }

    CDRAGON_BASE = "https://raw.communitydragon.org/latest/game/assets/ux/tft/championsplashes/patching"

    def get_tft16_icon_url(character_id: str) -> str:
        base = character_id.lower()
        return f"{CDRAGON_BASE}/{base}_square.tft_set16.png"
    
    async def build_comp_image(units):
        size = 80
        star_band_height = 30
        champ_imgs = []
        tiers = []

        emoji_font_path = r"C:\Windows\Fonts\seguiemj.ttf"
        try:
            font = ImageFont.truetype(emoji_font_path, 20)
        except Exception:
            font = ImageFont.load_default()

        async with aiohttp.ClientSession() as session:
            for u in units:
                char_id = u.get("character_id")
                if not char_id:
                    continue

                url = get_tft16_icon_url(char_id)
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.read()
                except:
                    continue

                try:
                    img = Image.open(BytesIO(data)).convert("RGBA")
                    img = img.resize((size, size))

                    champ_imgs.append(img)
                    tiers.append(u.get("tier", 1))
                except:
                    continue

        if not champ_imgs:
            return None

        # tier
        def tier_to_emoji(tier: int) -> str:
            if tier == 1:
                return "⭐"
            elif tier == 2:
                return "⭐⭐"
            else:
                return "⭐⭐⭐"

        # image finale
        width = size * len(champ_imgs)
        height = star_band_height + size
        final_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(final_img)

        for idx, img in enumerate(champ_imgs):
            x = idx * size

            stars_text = tier_to_emoji(tiers[idx])

            try:
                bbox = draw.textbbox((0, 0), stars_text, font=font)
                text_w = bbox[2] - bbox[0]
                text_h = bbox[3] - bbox[1]
            except AttributeError:
                text_w, text_h = font.getsize(stars_text)

            text_x = x + (size - text_w) // 2
            text_y = (star_band_height - text_h) // 2

            draw.text((text_x + 1, text_y + 1), stars_text,
                    fill=(0, 0, 0, 255), font=font)
            draw.text((text_x, text_y), stars_text,
                    fill=(255, 255, 255, 255), font=font)

            final_img.paste(img, (x, star_band_height), img)

        buf = BytesIO()
        final_img.save(buf, format="PNG")
        buf.seek(0)
        return buf

    # ---------- Envoi des embeds ----------
    for i, m in enumerate(matches, 1):
        placement = m["placement"]
        emoji = PLACEMENT_EMOJIS.get(placement, "")
        time_min = round(m["time_eliminated"] / 60)

        units = m.get("units", [])

        embed = discord.Embed(
            title=f"Partie classée #{i} — Top {placement} {emoji}",
            color=0x9b59b6
        )

        embed.add_field(
            name="Temps élimination",
            value=f"{time_min} min",
            inline=False
        )

        embed.add_field(
            name="Composition",
            value="(voir ci-dessous 👇)",
            inline=False
        )

        embed.set_footer(text="Top 1 = incroyable 🥇! Top 8 = dommage 😭...")

        comp_buf = await build_comp_image(units)

        if comp_buf is not None:
            filename = f"comp_{i}.png"
            file = discord.File(comp_buf, filename=filename)
            embed.set_image(url=f"attachment://{filename}")
            await ctx.send(embed=embed, file=file)
        else:
            await ctx.send(embed=embed)

@bot.command(aliases=["helpme", "commands"])
async def commande(ctx):
    embed = discord.Embed(
        title="📘 Commandes disponibles",
        color=0x2ecc71
    )

    embed.add_field(
        name="➕ !add <pseudo#tag>",
        value="Ajoute un joueur au classement.\n**Exemple :** `!add Toto#EUW`",
        inline=False
    )

    embed.add_field(
        name="➖ !remove <pseudo>",
        value="Retire un joueur du classement.\n**Exemple :** `!remove Toto`",
        inline=False
    )

    embed.add_field(
        name="📈 !stats <pseudo>",
        value="Liste quelques statistiques sur le joueur.\n**Exemple :** `!stats Toto`",
        inline=False
    )

    embed.add_field(
        name="🏆 !classement",
        value="Affiche le classement des joueurs ajoutés.\n**Exemple :** `!classement`",
        inline=False
    )

    embed.add_field(
        name="📋 !liste",
        value="Liste les joueurs suivis.\n**Exemple :** `!liste`",
        inline=False
    )

    embed.add_field(
        name="⚔️ !compare <pseudo1> vs <pseudo2>",
        value="Compare deux joueurs.\n**Exemple :** `!compare Jean Claude vs Claude Jean`",
        inline=False
    )

    embed.add_field(
        name="📜 !history <pseudo>",
        value="Affiche les 5 dernières games.\n**Exemple :** `!history Toto`",
        inline=False
    )

    embed.add_field(
        name="💀 !removeAll",
        value="Supprime totalement le classement. A ne pas utiliser n'importe comment.",
        inline=False
    )

    await ctx.send(embed=embed)


bot.run(DISCORD_TOKEN)
