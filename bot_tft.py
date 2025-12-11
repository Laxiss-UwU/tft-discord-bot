import discord
from discord.ext import commands
import aiohttp
import json
import os
import re
from io import BytesIO
from PIL import Image, ImageFont, ImageDraw
import asyncio

# CONFIG (change ici)
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
RIOT_API_KEY = os.getenv("RIOT_API_KEY")
REGION = 'euw1'
DATA_FILE = '/data/players.json'
STATS_FILE = '/data/stats.json'
CDRAGON_BASE = "https://raw.communitydragon.org/latest/game/assets/ux/tft/championsplashes/patching"

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

def load_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_stats(stats):
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

def _pretty_trait_name(trait_name: str) -> str:
    # "TFT16_Demacia" -> "Demacia"
    return trait_name.split("_")[-1].title()

async def analyze_comps(session, puuid: str, count: int = 60):
    """
    Analyse les dernières parties du joueur (set 16 uniquement) et renvoie :
    {
      "Compo": {"games": x, "wins": y, "placements": [...]},
      ...
    }
    On parallélise les appels à get_match_data pour accélérer.
    """
    comp_stats = {}

    match_ids = await get_match_ids(session, puuid, count)
    if not match_ids:
        return comp_stats

    # Limite de concurrence pour respecter le rate-limit Riot
    semaphore = asyncio.Semaphore(5)

    async def fetch_match(mid):
        async with semaphore:
            try:
                return await get_match_data(session, mid)
            except Exception:
                return None

    # On récupère les infos de match en parallèle
    results = await asyncio.gather(
        *(fetch_match(mid) for mid in match_ids),
        return_exceptions=False
    )

    for data in results:
        if not data:
            continue

        info = data.get("info", {})

        if info.get("queue_id") != 1100:
            continue

        # On récupère le participant correspondant
        participant = None
        for p in info.get("participants", []):
            if p.get("puuid") == puuid:
                participant = p
                break

        if not participant:
            continue

        placement = participant.get("placement")
        traits = participant.get("traits", [])
        if placement is None or not traits:
            continue

        # --------- FILTRE SET 16 UNIQUEMENT ----------
        if not any(t.get("name", "").startswith("TFT16_") for t in traits):
            continue

        # Trait "principal" : plus haut tier_current puis num_units
        main_trait = max(
            traits,
            key=lambda t: (t.get("tier_current", 0), t.get("num_units", 0))
        )
        comp_name = _pretty_trait_name(main_trait.get("name", "Unknown"))

        stats = comp_stats.setdefault(
            comp_name,
            {"games": 0, "wins": 0, "placements": []}
        )
        stats["games"] += 1
        stats["placements"].append(placement)

        if 1 <= placement <= 4:  # Top 1–4 = win
            stats["wins"] += 1

    return comp_stats

def _winrate(stats_dict) -> float:
    g = stats_dict["games"]
    if g == 0:
        return 0.0
    return round(stats_dict["wins"] * 100 / g, 1)


def _avg_placement(stats_dict) -> float:
    pl = stats_dict["placements"]
    if not pl:
        return 0.0
    return round(sum(pl) / len(pl), 2)

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

def get_default_font():
    try:
        import PIL
        font_path = os.path.join(os.path.dirname(PIL.__file__), "Tests/fonts/FreeMono.ttf")
        return ImageFont.truetype(font_path, 22)
    except Exception:
        return ImageFont.load_default()

def get_icon_url(character_id: str) -> str:
    return f"{CDRAGON_BASE}/{character_id.lower()}_square.tft_set16.png"

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

    # On charge le cache
    all_stats = load_stats()
    cached = all_stats.get(player["uuid"])

    async with aiohttp.ClientSession() as session:
        # Classement actuel
        league = await get_league(session, player['uuid'])

        # Compos : soit depuis le cache, soit on recalcule
        if cached and "comps" in cached:
            comp_stats = cached["comps"]
        else:
            comp_stats = await analyze_comps(session, player['uuid'], count=60)
            all_stats[player["uuid"]] = {
                "name": player["name"],
                "region": REGION,
                "comps": comp_stats,
            }
            save_stats(all_stats)

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

    # ---------- Bloc "compos" ----------
    if comp_stats:
        # compo la plus jouée
        most_played_name, most_played = max(
            comp_stats.items(),
            key=lambda item: item[1]["games"]
        )

        MIN_GAMES_FOR_WR = 3
        eligible = {
            n: s for n, s in comp_stats.items()
            if s["games"] >= MIN_GAMES_FOR_WR
        } or comp_stats

        best_name, best_stats = max(
            eligible.items(),
            key=lambda item: _winrate(item[1])
        )
        worst_name, worst_stats = min(
            eligible.items(),
            key=lambda item: _winrate(item[1])
        )

        most_avg = _avg_placement(most_played)
        best_avg = _avg_placement(best_stats)
        worst_avg = _avg_placement(worst_stats)

        most_wr = _winrate(most_played)
        best_wr = _winrate(best_stats)
        worst_wr = _winrate(worst_stats)

        line = (
            f"**Compo la plus jouée :** {most_played_name} "
            f"({most_played['games']} games) : AVG --> {most_avg} ({most_wr}% WR)\n"
            f"**Meilleure compo :** {best_name} "
            f"({best_stats['games']} games) : AVG --> {best_avg} ({best_wr}% WR)\n"
            f"**Pire compo :** {worst_name} "
            f"({worst_stats['games']} games) : AVG --> {worst_avg} ({worst_wr}% WR)"
        )

        embed.add_field(
            name="🍀 Data compos",
            value=line,
            inline=False
        )
    else:
        embed.add_field(
            name="🍀 Data compos",
            value="Pas assez de données récentes (set 16) pour analyser les compositions.",
            inline=False
        )

    embed.set_footer(text="Données issues de l'API Riot Games")

    await ctx.send(embed=embed)
    
@bot.command()
async def compare(ctx, *, args: str):
    players = re.findall(r'"([^"]+)"', args)

    if len(players) != 2:
        return await ctx.send("❌ Utilisation incorrecte.\nFormat : `!compare \"pseudo1\" \"pseudo2\"`")

    player1, player2 = players
    RANK_VALUES = {'IV': 0, 'III': 1, 'II': 2, 'I': 3}
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
    def score(tier, div, lp):
        return TIER_VALUES.get(tier, 0) * 1000 + RANK_VALUES.get(div, 0) * 100 + lp

    score_p1 = score(t1,d1,lp1)
    score_p2 = score(t2,d2,lp2)

    winner = player1 if score_p1 > score_p2 else player2
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
        name="⚔️ !compare \"pseudo1\" \"pseudo2\"",
        value="Compare deux joueurs.\n**Exemple :** `!compare \"Jean Claude\" \"Claude Jean\"`",
        inline=False
    )

    embed.add_field(
        name="📜 !history <pseudo>",
        value="Affiche les 5 dernières games.\n**Exemple :** `!history Toto`",
        inline=False
    )

    embed.add_field(
        name="📜 !ranked <pseudo>",
        value="Affiche les 5 dernières games ranked (sur les 20 dernières games).\nExemple : !ranked Toto",
        inline=False
    )

    embed.add_field(
        name="🤓 !nolife",
        value="Affiche le classement des 10 joueurs avec le plus de parties cette saison.",
        inline=False
    )

    embed.add_field(
        name="💀 !removeAll",
        value="Supprime totalement le classement. A ne pas utiliser n'importe comment.",
        inline=False
    )

    await ctx.send(embed=embed)

@bot.command(aliases=["ranked_history"])
async def ranked(ctx, *, name: str):
    name = name.strip()
    if not name:
        return await ctx.send("❌ Tu dois préciser un pseudo. Exemple : `!ranked Toto`")

    players = load_players()
    player = next((p for p in players if p["name"].lower() == name.lower()), None)
    if not player:
        return await ctx.send(f"❌ **{name}** n'est pas dans la liste.")

    # Récupérer les 20 dernières parties, filtrer les 5 ranked les plus récentes
    async with aiohttp.ClientSession() as session:
        match_ids = await get_match_ids(session, player["uuid"], 20)
        if not match_ids:
            return await ctx.send("❌ Impossible de récupérer l'historique.")

        ranked_matches = []
        for match_id in match_ids:
            data = await get_match_data(session, match_id)
            if not data:
                continue
            info = data.get("info", {})
            if info.get("queue_id") != 1100:  # only ranked
                continue
            for pinfo in info.get("participants", []):
                if pinfo["puuid"] == player["uuid"]:
                    ranked_matches.append(pinfo)
                    break
            if len(ranked_matches) >= 5:
                break

    if not ranked_matches:
        return await ctx.send(f"⚪ **{name}** n'a pas joué de ranked dans ses 20 dernières parties.")

    # Emojis de placement
    PLACEMENT_EMOJIS = {
        1: "🥇", 2: "🥈", 3: "🥉",
        4: "🙂", 5: "🙃", 6: "😥", 7: "😢", 8: "😭"
    }

    # Génère l'image compacte d'une compo (étoiles en '*')
    async def build_comp_image(units):
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont

        size = 80
        star_band_height = 28  # bande au dessus des icônes pour les étoiles

        champ_imgs = []
        tiers = []

        async with aiohttp.ClientSession() as sub_session:
            for u in units:
                cid = u.get("character_id")
                if not cid:
                    continue
                url = get_icon_url(cid)
                try:
                    async with sub_session.get(url) as resp:
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

        def tier_str(t):
            t = max(1, min(3, int(t)))
            return "*" * t

        width = size * len(champ_imgs)
        height = star_band_height + size
        final_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(final_img)

        # Police sûre
        try:
            # prefer builtin pillow test font (toujours présent)
            import PIL as _PIL
            font_path = os.path.join(os.path.dirname(_PIL.__file__), "Tests/fonts/FreeMono.ttf")
            font = ImageFont.truetype(font_path, 14)
        except Exception:
            font = ImageFont.load_default()

        for idx, champ_img in enumerate(champ_imgs):
            x = idx * size
            stars = tier_str(tiers[idx])

            # Mesure du texte avec textbbox (compat Pillow 10+)
            bbox = draw.textbbox((0, 0), stars, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            tx = x + (size - text_w) // 2
            ty = (star_band_height - text_h) // 2

            # contour noir (4 directions) + texte blanc
            draw.text((tx + 1, ty + 1), stars, fill=(0, 0, 0), font=font)
            draw.text((tx - 1, ty + 1), stars, fill=(0, 0, 0), font=font)
            draw.text((tx + 1, ty - 1), stars, fill=(0, 0, 0), font=font)
            draw.text((tx - 1, ty - 1), stars, fill=(0, 0, 0), font=font)
            draw.text((tx, ty), stars, fill=(255, 255, 255), font=font)

            final_img.paste(champ_img, (x, star_band_height), champ_img)

        buf = BytesIO()
        final_img.save(buf, format="PNG")
        buf.seek(0)
        return buf

    # Pour chaque ranked, envoi d'un embed compact (titre + image)
    for idx, m in enumerate(ranked_matches, 1):
        placement = m.get("placement", 0)
        emoji = PLACEMENT_EMOJIS.get(placement, "")
        minutes = round(m.get("time_eliminated", 0) / 60)
        units = m.get("units", [])

        # build image
        comp_buf = await build_comp_image(units)

        title = f"Game #{idx} — TOP {placement} {emoji} — ⏱️ {minutes} min"
        embed = discord.Embed(title=title, color=0x9b59b6)

        if comp_buf:
            fname = f"comp_{player['name']}_{idx}.png"
            file = discord.File(comp_buf, filename=fname)
            embed.set_image(url=f"attachment://{fname}")
            await ctx.send(embed=embed, file=file)
        else:
            # sans image, on envoie juste l'embed minimal
            embed.description = "Aucune image de compo disponible."
            await ctx.send(embed=embed)

@bot.command()
async def nolife(ctx):
    players = load_players()
    if not players:
        return await ctx.send("❌ Aucun joueur enregistré.")

    results = []

    async with aiohttp.ClientSession() as session:
        for p in players:
            name = p["name"]
            puuid = p["uuid"]

            league = await get_league(session, puuid)
            if not league:
                continue  # joueur unranked → pas de stats

            wins = league.get("wins", 0)
            losses = league.get("losses", 0)
            total = wins + losses

            results.append((name, total, wins, losses))

    if not results:
        return await ctx.send("⚪ Aucun joueur n'a de parties classées.")

    # Tri décroissant par total de parties
    results.sort(key=lambda x: x[1], reverse=True)

    top10 = results[:10]

    embed = discord.Embed(
        title="🏆 TOP 10 des plus gros no-life TFT",
        description="Classement basé sur le total **de parties classées jouées**.\n",
        color=0xe67e22
    )

    medals = ["🥇", "🥈", "🥉"]

    lines = []
    for i, (name, total, wins, losses) in enumerate(top10, start=1):
        medal = medals[i-1] if i <= 3 else f"#{i}"
        lines.append(
            f"**{medal} — {name}** : `{total}` games "
            f"(🔵 {wins} / 🔴 {losses})"
        )

    embed.add_field(name="Classement", value="\n".join(lines), inline=False)
    embed.set_footer(text="Basé sur les statistiques classées Riot Games")

    await ctx.send(embed=embed)


bot.run(DISCORD_TOKEN)
