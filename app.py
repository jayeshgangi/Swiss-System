from flask import Flask, render_template, request, redirect, session, url_for, jsonify
import os, json, random
from collections import defaultdict
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.secret_key = "#JAYESH"


basedir = os.path.abspath(os.path.dirname(__file__))

# Create db folder if it doesn't exist
db_folder = os.path.join(basedir, 'db')
if not os.path.exists(db_folder):
    os.makedirs(db_folder)
    print(f"Created database folder: {db_folder}")

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'db', 'db.sqlite3')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ------------------- Database Models -------------------

class Tournament(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    rounds = db.Column(db.Integer, nullable=False)
    max_players = db.Column(db.Integer, nullable=False, default=10)
    win_points = db.Column(db.Float, default=1.0)
    draw_points = db.Column(db.Float, default=0.5)
    loss_points = db.Column(db.Float, default=0.0)
    participants = db.relationship('Participant', backref='tournament', lazy=True, cascade='all, delete-orphan')
    rounds_data = db.relationship('Round', backref='tournament', lazy=True, cascade='all, delete-orphan')

class Participant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    elo = db.Column(db.Integer, default=1000)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournament.id'), nullable=False)
    score = db.Column(db.Float, default=0.0)
    opponents = db.Column(db.Text, default="[]")
    white_count = db.Column(db.Integer, default=0)
    black_count = db.Column(db.Integer, default=0)
    last_colors = db.Column(db.Text,default="[]")
    float_history = db.Column(db.Text,default="[]")
    bye_count = db.Column(db.Integer,default=0)

class Round(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournament.id'), nullable=False)
    round_number = db.Column(db.Integer, nullable=False)
    pairings = db.Column(db.Text, default="[]")
    bye_player_id = db.Column(db.Text, default="[]")

# Initialize database - run this once to create tables
with app.app_context():
    db.create_all()
    print("Database tables created successfully!")

def serialize_participant_data(participants):
    """Convert all list attributes to JSON strings for database storage"""
    for p in participants:
        if hasattr(p, 'last_colors') and isinstance(p.last_colors, list):
            p.last_colors = json.dumps(p.last_colors)
        if hasattr(p, 'float_history') and isinstance(p.float_history, list):
            p.float_history = json.dumps(p.float_history)
        if hasattr(p, 'opponents_list') and isinstance(p.opponents_list, list):
            p.opponents = json.dumps(p.opponents_list)

# ------------------- Swiss Pairing Logic -------------------

def swiss_pairings_participants(participants, round_number):
    """
    FIDE Dutch Swiss System with corrected color preference & pairing behavior.

    Key fixes:
    - Proper classification of absolute / strong / mild preferences.
    - Odd-round: treat 'strong' as 'absolute' (promote strong->absolute).
    - Even-round: allow mild preferences (when player has even games played)
      to be adjusted to reduce same-strong-color pairings.
    - assign_colors follows FIDE priority order (absolute > strong > mild >
      higher-ranked player's preference > fallback).
    - Improved would_violate logic (prevents 3 same-colors-in-a-row and
      prevents creating extreme imbalance).
    - Pairing tries opposite preferences first, then mixes, then unavoidable same-pref pairs.
    - Maintains float_history and bye logic similar to your original.
    """
    bye_player=None
    # -------------------- INIT --------------------
    for p in participants:

        p.opponents_list = json.loads(p.opponents) if getattr(p, 'opponents', None) else []
        p.last_colors = json.loads(p.last_colors) if getattr(p, 'last_colors', None) else []
        p.float_history = json.loads(p.float_history) if getattr(p, 'float_history', None) else []
        p.white_count = int(getattr(p, 'white_count', 0))
        p.black_count = int(getattr(p, 'black_count', 0))
        p.color_diff = p.white_count - p.black_count

    def ensure_list(attr):
        return json.loads(attr) if isinstance(attr, str) else attr


    # -------------------- COLOR PREF FUNCTIONS --------------------
    def get_color_preference(player,round_number):
        """
        Return a dict: {'type': 'absolute'|'strong'|'mild'|None,
                        'color': 'white'|'black'|None,
                        'games_played': int,
                        'mild_adjustable': bool}
        - mild_adjustable will be True for even rounds & even games_played (per FIDE note)
          meaning the mild preference can be flipped in even rounds to reduce strong-strong clashes.
        """
        games_played = player.white_count + player.black_count
        diff = player.color_diff
        last_colors = player.last_colors or []
        pref_type = None
        pref_color = None
        mild_adjustable = False

        if games_played == 0:
            return {'type': None, 'color': None, 'games_played': 0, 'mild_adjustable': False}

        # ABSOLUTE: color difference > +1 or < -1 OR last two same color
        if diff >= 2:
            pref_type = 'absolute'
            pref_color = 'black'
        elif diff <= -2:
            pref_type = 'absolute'
            pref_color = 'white'
        elif len(last_colors) >= 2 and last_colors[-1] == last_colors[-2]:
            # If last two were same, preference is opposite color (absolute)
            pref_type = 'absolute'
            pref_color = 'white' if last_colors[-1] == 'black' else 'black'
        else:
            # STRONG if diff == +1 or -1
            if diff == 1:
                pref_type = 'strong'
                pref_color = 'black'
            elif diff == -1:
                pref_type = 'strong'
                pref_color = 'white'
            else:
                # MILD: diff == 0 or (no clear diff) -> alternate from last game
                pref_type = 'mild'
                if last_colors:
                    pref_color = 'black' if last_colors[-1] == 'white' else 'white'
                else:
                    # By convention if no last color, mild prefer white (as before)
                    pref_color = 'white'

        # Apply odd-round promotion: strong → absolute
        if round_number % 2 == 1 and pref_type == 'strong':
            pref_type = 'absolute'

        # Even-round mild adjustable
        if round_number % 2 == 0 and pref_type == 'mild' and games_played % 2 == 0:
            mild_adjustable = True

        return {
            'type': pref_type,
            'color': pref_color,
            'games_played': games_played,
            'mild_adjustable': mild_adjustable
        }

    def would_violate_color_rules(player, assigned_color,opponent=None):
        """Check if assigning this color would violate rules."""
        
        new_diff = player.color_diff + (1 if assigned_color == 'white' else -1)
        
        # Rule 2: Can't have same color 3 times in a row
        if len(player.last_colors) >= 2:
            if player.last_colors[-1] == player.last_colors[-2] == assigned_color:
                return True
        
        # Rule 3: Check absolute preference (CRITICAL FIX)
        pref = get_color_preference(player, round_number)
        if pref['type'] == 'absolute' and pref['color'] and pref['color'] != assigned_color:
            return True
        
        return False

    def can_pair(p1, p2):
        """Check basic pairing legality: not previous opponents and color absolute conflict."""
        if p2.id in p1.opponents_list or p1.id in p2.opponents_list:
            return False

        pref1 = get_color_preference(p1,round_number)
        pref2 = get_color_preference(p2,round_number)

        # Only disallow if absolutely cannot assign colors
        for c1, c2 in [('white', 'black'), ('black', 'white')]:
            if not would_violate_color_rules(p1, c1,opponent=p2) and not would_violate_color_rules(p2, c2,opponent=p1):
                return True
        return False

    def colors_are_compatible(p1, p2):
        """
        Quick check: do the preferences want opposite colors?
        If any has None preference, treat as compatible.
        Takes into account promotion for odd-round inside get_color_preference.
        """
        pref1 = get_color_preference(p1,round_number)
        pref2 = get_color_preference(p2,round_number)
        if pref1['color'] is None or pref2['color'] is None:
            return True
        return pref1['color'] != pref2['color']

    def calculate_pairing_quality(p1, p2):
        """
        Heuristic quality measure (lower = better):
        - Primary: minimize score difference (strict)
        - Secondary: try to satisfy absolute/strong preferences by penalizing if they'd conflict
        - Tertiary: prefer opposite preference pairs
        - Additional: float penalties to discourage bad float directions
        This is a heuristic used only to choose among many legal pairings.
        """
        score = 0
        # Strong primary penalty for score difference so we don't pair widely separated players
        score += abs(p1.score - p2.score) * 100000

        pref1 = get_color_preference(p1,round_number)
        pref2 = get_color_preference(p2,round_number)

        # If one has absolute preference that would be violated by pairing assignment choices,
        # add big penalty. We'll check both assignment directions.
        # If there is at least one assignment direction that respects absolute prefs, it's okay.
        absolute_violation = True
        for c1, c2 in [('white', 'black'), ('black', 'white')]:
            if pref1['type'] == 'absolute' and pref1['color'] != c1:
                continue
            if pref2['type'] == 'absolute' and pref2['color'] != c2:
                continue
            if would_violate_color_rules(p1, c1,opponent=p2) or would_violate_color_rules(p2, c2,opponent=p1):
                continue
            # found a legal assignment that doesn't violate absolute pref
            absolute_violation = False
            break
        if absolute_violation:
            score += 50000

        # Penalize if both want the same color (makes pairing less desirable)
        if pref1['color'] and pref2['color'] and pref1['color'] == pref2['color']:
            # heavier if one of them is absolute / strong
            if pref1['type'] == 'absolute' or pref2['type'] == 'absolute':
                score += 40000
            elif pref1['type'] == 'strong' or pref2['type'] == 'strong':
                score += 5000
            else:
                score += 1000
        else:
            # bonus slightly if they want opposite colors
            if pref1['color'] and pref2['color'] and pref1['color'] != pref2['color']:
                score -= 500

        # Float heuristics (avoid up-floating a lower player with a much higher score, etc.)
        if p1.float_history and p1.float_history[-1] == 'down' and p1.score > p2.score:
            score += 200
        if p2.float_history and p2.float_history[-1] == 'up' and p2.score < p1.score:
            score += 200

        return score

    def assign_colors(p1, p2, round_number):
        """
        Assign colors following FIDE priority order.
        Returns (white_player, black_player)
        """
        pref1 = get_color_preference(p1, round_number)
        pref2 = get_color_preference(p2, round_number)

        def valid_assignment(white, black):
            return (not would_violate_color_rules(white, 'white', opponent=black) and 
                    not would_violate_color_rules(black, 'black', opponent=white))

        # Priority 1: Both absolute with opposite preferences
        if pref1['type'] == 'absolute' and pref2['type'] == 'absolute':
            if pref1['color'] == 'white' and pref2['color'] == 'black':
                if valid_assignment(p1, p2):
                    return p1, p2
            elif pref1['color'] == 'black' and pref2['color'] == 'white':
                if valid_assignment(p2, p1):
                    return p2, p1

        # Priority 2: One absolute preference
        if pref1['type'] == 'absolute' and pref1['color']:
            if pref1['color'] == 'white' and valid_assignment(p1, p2):
                return p1, p2
            elif pref1['color'] == 'black' and valid_assignment(p2, p1):
                return p2, p1

        if pref2['type'] == 'absolute' and pref2['color']:
            if pref2['color'] == 'white' and valid_assignment(p2, p1):
                return p2, p1
            elif pref2['color'] == 'black' and valid_assignment(p1, p2):
                return p1, p2

        # Priority 3: Both strong with opposite preferences
        if pref1['type'] == 'strong' and pref2['type'] == 'strong':
            if pref1['color'] == 'white' and pref2['color'] == 'black':
                if valid_assignment(p1, p2):
                    return p1, p2
            elif pref1['color'] == 'black' and pref2['color'] == 'white':
                if valid_assignment(p2, p1):
                    return p2, p1

        # Priority 4: One strong preference
        if pref1['type'] == 'strong' and pref1['color']:
            if pref1['color'] == 'white' and valid_assignment(p1, p2):
                return p1, p2
            elif pref1['color'] == 'black' and valid_assignment(p2, p1):
                return p2, p1

        if pref2['type'] == 'strong' and pref2['color']:
            if pref2['color'] == 'white' and valid_assignment(p2, p1):
                return p2, p1
            elif pref2['color'] == 'black' and valid_assignment(p1, p2):
                return p1, p2

        # Priority 5: Both mild with opposite preferences
        if pref1['type'] == 'mild' and pref2['type'] == 'mild':
            if pref1['color'] == 'white' and pref2['color'] == 'black':
                if valid_assignment(p1, p2):
                    return p1, p2
            elif pref1['color'] == 'black' and pref2['color'] == 'white':
                if valid_assignment(p2, p1):
                    return p2, p1
        
        # Priority 6: One mild preference
        if pref1['type'] == 'mild' and pref1['color']:
            if pref1['color'] == 'white' and valid_assignment(p1, p2):
                return p1, p2
            elif pref1['color'] == 'black' and valid_assignment(p2, p1):
                return p2, p1

        if pref2['type'] == 'mild' and pref2['color']:
            if pref2['color'] == 'white' and valid_assignment(p2, p1):
                return p2, p1
            elif pref2['color'] == 'black' and valid_assignment(p1, p2):
                return p1, p2

        # Priority 7: Higher-ranked player preference
        higher = p1 if (p1.score > p2.score or (p1.score == p2.score and getattr(p1, 'elo', 0) >= getattr(p2, 'elo', 0))) else p2
        lower = p2 if higher == p1 else p1
        
        h_pref = get_color_preference(higher, round_number)
        if h_pref['color'] == 'white' and valid_assignment(higher, lower):
            return higher, lower
        elif h_pref['color'] == 'black' and valid_assignment(lower, higher):
            return lower, higher

        # Priority 8: Minimize color imbalance
        candidates = []
        for (w, b) in [(p1, p2), (p2, p1)]:
            if valid_assignment(w, b):
                w_new_diff = abs((w.white_count + 1) - w.black_count)
                b_new_diff = abs(b.white_count - (b.black_count + 1))
                candidates.append(((w, b), w_new_diff + b_new_diff))
        
        if candidates:
            candidates.sort(key=lambda x: x[1])
            return candidates[0][0]

        # Fallback
        if valid_assignment(p1, p2):
            return p1, p2
        return p2, p1

    def select_bye_player(players):
        """Select bye recipient - lowest score, fewest byes, hasn't had bye recently; tie-break on higher id."""
        # Prioritize players who haven't had a bye yet (bye_count = 0)
        eligible = [p for p in players if getattr(p, 'bye_count', 0) == 0]
        
        if not eligible:
            # If everyone has had at least one bye, pick the one with fewest byes
            eligible = players[:]
        
        # Sort by: lowest score first, then fewest byes, then highest ID (for tiebreak)
        eligible.sort(key=lambda x: (x.score, getattr(x, 'bye_count', 0), -x.id))
        return eligible[0]


    
    def swiss_pairings_round_1(participants):
        """
        Round 1 special pairing: Sort by ELO, pair top half vs bottom half.
        Highest ELO plays against median ELO.
        Returns list of pairings (no bye in round 1 for even players).
        """
        # Sort by ELO (highest first), then by ID for tiebreak
        sorted_players = sorted(participants, key=lambda x: (-getattr(x, 'elo', 1000), x.id))
    
        n = len(sorted_players)
        pairs = []
    
        for i in range(0, n, 2):
            if i + 1 < n: 
                p1 = sorted_players[i]
                p2 = sorted_players[i+1]
                pairs.append((p1, p2))
    
        return pairs

    # -------------------- BRACKET PAIRING --------------------
    def pair_bracket_with_color_priority(players):
        """
        Pair players inside a score bracket while prioritizing satisfying absolute/strong prefs
        and trying to match opposite preferences first.
        Returns (pairs_list, floaters_list)
        """
        if len(players) < 2:
            return [], players[:]

        # sort by FIDE typical order: higher score first (already bracket), then higher elo, lower id last
        players = sorted(players, key=lambda x: (-x.score, -getattr(x, 'elo', 0), x.id))

        n = len(players)
        pairs = []
        used = set()

            # Split into top and bottom half for initial attempt
        mid = n // 2
        top_half = [p for p in players[:mid] if p.id not in used]
        bottom_half = [p for p in players[mid:] if p.id not in used]
    
        # Try pairing top half with bottom half first (classic Swiss approach)
        for i, p_top in enumerate(top_half):
            if p_top.id in used:
                continue
        
            # Try to find best match from bottom half
            best_partner = None
            best_quality = float('inf')

            for p_bottom in bottom_half:
                if p_bottom.id in used:
                    continue
                if not can_pair(p_top, p_bottom):
                    continue
                try:
                    white,black = assign_colors(p_top,p_bottom,round_number)
                except Exception:
                    continue
            
                quality = calculate_pairing_quality(white, black)
                if quality < best_quality:
                    best_quality = quality
                    best_partner = p_bottom
        
            if best_partner:
                pairs.append((p_top, best_partner))
                used.add(p_top.id)
                used.add(best_partner.id)
    
        # For remaining unpaired players, use consecutive pairing with lookahead
        remaining = [p for p in players if p.id not in used]

        i=0
        while i < len(remaining) -1:
            p1=remaining[i]
            if p1.id in used:
                i +=1
                continue

            # Try next available partners (lookahead up to 5 positions)
            best_partner = None
            best_quality = float('inf')
            
            for j in range(i+1,len(remaining)):
                    p2 = remaining[j]
                    if p2.id in used:
                        continue

                    if not can_pair(p1,p2):
                        continue

                    quality = calculate_pairing_quality(p1,p2)

                    # Slight preference for consecutive pairing (maintain bracket order)
                    if j == i + 1:
                        quality -= 500
            
                    if quality < best_quality:
                        best_quality = quality
                        best_partner = p2
        
            if best_partner:
                pairs.append((p1, best_partner))
                used.add(p1.id)
                used.add(best_partner.id)

            i +=1
        floaters = [p for p in players if p.id not in used]  # leftover unpaired players
        return pairs, floaters

    # -------------------- MAIN --------------------

    # SPECIAL CASE: ROUND 1 - Pair by ELO
    if round_number == 1:
        participants_for_pairing=participants[:]
        if len(participants) % 2 == 1:
            bye_player = select_bye_player(participants)
            participants_for_pairing = [p for p in participants if p.id != bye_player.id]
            bye_player.bye_count = getattr(bye_player, 'bye_count', 0) + 1  # ✅ CORRECT
            bye_player.score += 1.0

            bye_player.float_history.append('down')

        all_pairs = swiss_pairings_round_1(participants_for_pairing)
        
        # Assign colors for round 1 (simple alternation or random)
        pairings = []
        for p1, p2 in all_pairs:
            # For round 1, higher ELO gets white (or alternate)
            if getattr(p1, 'elo', 1000) >= getattr(p2, 'elo', 1000):
                white, black = p1, p2
            else:
                white, black = p2, p1
            
            # Update opponents
            p1.opponents_list.append(p2.id)
            p2.opponents_list.append(p1.id)
            p1.opponents = json.dumps(p1.opponents_list)
            p2.opponents = json.dumps(p2.opponents_list)
            
            # Update colors
            white.white_count += 1
            black.black_count += 1
            white.color_diff = white.white_count - white.black_count
            black.color_diff = black.white_count - black.black_count

            # Ensure lists before appending
            white.last_colors = ensure_list(white.last_colors)
            black.last_colors = ensure_list(black.last_colors)
            white.float_history = ensure_list(white.float_history)
            black.float_history = ensure_list(black.float_history)



            white.last_colors.append('white')
            black.last_colors.append('black')
            
            white.last_colors = json.dumps(white.last_colors)
            black.last_colors = json.dumps(black.last_colors)
            white.float_history = json.dumps(white.float_history)
            black.float_history = json.dumps(black.float_history)
            
            pairings.append({
                "white_id": white.id,
                "white_name": getattr(white, 'name', None),
                "black_id": black.id,
                "black_name": getattr(black, 'name', None),
                "result": None
            })
        
        serialize_participant_data(participants)
        
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"Round 1 commit error: {e}")
            raise
        
        return pairings, bye_player

    # ROUNDS 2+: Standard Swiss system by score brackets
    sorted_players = sorted(participants, key=lambda x: (-x.score, -getattr(x, 'elo', 0), x.id))
    score_brackets = defaultdict(list)
    for p in sorted_players:
        score_brackets[p.score].append(p)
    scores = sorted(score_brackets.keys(), reverse=True)

    all_pairs = []
    floaters = []

    all_players = sorted_players[:]
    if len(all_players) % 2 == 1:
        bye_player = select_bye_player(all_players)
        # Remove bye player for this round's pairing
        all_players.remove(bye_player)
        # Mark bye
        bye_player.bye_count = getattr(bye_player, 'bye_count', 0) + 1  # ✅ Increment count


        if getattr(bye_player,'tournament',None):
            bye_player.score += getattr(bye_player.tournament, 'win_points',1.0)
        else:
            bye_player.score += 1.0

        bye_player.float_history.append('down')
        
    # Process each score bracket
    for score in scores:
        bracket_players = [p for p in score_brackets[score] if p in all_players]
        # add floaters from previous higher bracket
        bracket_players.extend(floaters)
        floaters = []

        if len(bracket_players) < 2:
            floaters = bracket_players
            continue

        pairs, new_floaters = pair_bracket_with_color_priority(bracket_players)

        # update float history
        for p1, p2 in pairs:

            p1.float_history = ensure_list(p1.float_history)
            p2.float_history = ensure_list(p2.float_history)
            p1.last_colors = ensure_list(p1.last_colors)
            p2.last_colors = ensure_list(p2.last_colors)
            if p1.score > p2.score:
                p1.float_history.append('down')
                p2.float_history.append('up')
            elif p2.score > p1.score:
                p2.float_history.append('down')
                p1.float_history.append('up')
            else:
                p1.float_history.append(None)
                p2.float_history.append(None)

            p1.float_history = json.dumps(p1.float_history)
            p2.float_history = json.dumps(p2.float_history)
            p1.last_colors = json.dumps(p1.last_colors)
            p2.last_colors = json.dumps(p2.last_colors)

        all_pairs.extend(pairs)
        floaters = new_floaters

    # Try to pair remaining floaters across brackets if possible
    if len(floaters) >= 2:
        remaining_pairs, leftover = pair_bracket_with_color_priority(floaters)

        for p1, p2 in remaining_pairs:
            if p1.score > p2.score:
                p1.float_history.append('down')
                p2.float_history.append('up')
            elif p2.score > p1.score:
                p2.float_history.append('down')
                p1.float_history.append('up')
            else:
                p1.float_history.append(None)
                p2.float_history.append(None)
        
        all_pairs.extend(remaining_pairs)
        floaters = leftover
# -------------------- FINALIZE PAIRINGS -----------------
    pairings=[]
    for p1, p2 in all_pairs:
        white, black = assign_colors(p1, p2,round_number)

        # update opponents lists
        p1.opponents_list.append(p2.id)
        p2.opponents_list.append(p1.id)

        # update color counts and last_colors
        white.white_count = getattr(white, 'white_count', 0) + 1
        black.black_count = getattr(black, 'black_count', 0) + 1
        white.color_diff = white.white_count - white.black_count
        black.color_diff = black.white_count - black.black_count

        white.last_colors = ensure_list(white.last_colors)
        black.last_colors = ensure_list(black.last_colors)

        
        #CRITICAL FIX: Append to list, then convert to JSON string
        white.last_colors.append('white')
        black.last_colors.append('black')
    
        pairings.append({
            "white_id": white.id,
            "white_name": getattr(white, 'name', None),
            "black_id": black.id,
            "black_name": getattr(black, 'name', None),
            "result": None
        })

    serialize_participant_data(participants)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print("Error committing pairings: {e}")
        raise

    return pairings, bye_player

# ------------------- Helper Functions -------------------

def get_current_round_number(tournament_id):
    last_round = Round.query.filter_by(tournament_id=tournament_id).order_by(Round.round_number.desc()).first()
    return last_round.round_number if last_round else 0

def get_round_data(tournament_id, round_number):
    # ✅ FIXED: Use correct column names
    rnd = Round.query.filter_by(tournament_id=tournament_id, round_number=round_number).first()
    if not rnd:
        return [], [], {}
    
    pairings = json.loads(rnd.pairings) if rnd.pairings else []
    bye_players = []  # ✅ List to handle multiple bye players

    # ✅ FIXED: Use correct column name and handle multiple byes
    if getattr(rnd, 'bye_player_id', None):
        try:
            bye_ids = json.loads(rnd.bye_player_id)
            for bye_id in bye_ids:
                if bye_id is not None:
                    player = Participant.query.get(bye_id)
                    if player:
                        bye_players.append(player)
        except Exception as e:
            print(f"Error loading bye players: {e}")
            bye_players = []

    results = {}
    for p in pairings:
        key = f"{p['white_id']}-{p['black_id']}"
        results[key] = p.get("result")
    
    return pairings, bye_players, results

def save_round_pairings(tournament_id, round_number, pairings, bye_players):
    # ✅ FIXED: Use correct column names and handle multiple bye players
    rnd = Round.query.filter_by(tournament_id=tournament_id, round_number=round_number).first()
    if not rnd:
        rnd = Round(tournament_id=tournament_id, round_number=round_number)
        db.session.add(rnd)
    
    rnd.pairings = json.dumps(pairings)
    
    # ✅ Handle multiple bye players
    if bye_players:
        # bye_players can be a list of Participant objects or a single Participant
        if isinstance(bye_players, list):
            rnd.bye_player_id = json.dumps([p.id for p in bye_players])
        else:
            # Single player passed
            rnd.bye_player_id = json.dumps([bye_players.id])
    else:
        rnd.bye_player_id = json.dumps([])
    
    db.session.commit()

def load_rounds(tournament_id):
    rounds = []
    current_round = get_current_round_number(tournament_id)
    
    for r in range(1, current_round + 1):
        pairings, bye_players, results = get_round_data(tournament_id, r)
        rounds.append({
            "round_number": r,  # ✅ FIXED: Use round_number, not roundnumber
            "pairings": pairings,
            "bye_players": [p.name for p in bye_players] if bye_players else [],  # ✅ Multiple players
            "results": results
        })
    
    return rounds

def generate_next_round(tournament_id):
    """Generate next round only if current round is complete"""
    participants = Participant.query.filter_by(tournament_id=tournament_id).all()
    if not participants:
        return False, "No participants found"
    
    current_round_num = get_current_round_number(tournament_id)
    
    # Check if current round results are saved (if rounds exist)
    if current_round_num > 0:
        pairings, bye_players, results = get_round_data(tournament_id, current_round_num)
        
        # Check if all matches have results
        for pairing in pairings:
            if not pairing.get('result'):
                return False, f"Please save Round {current_round_num} results before generating next round"
    
    round_number = current_round_num + 1
    tournament = Tournament.query.get(tournament_id)
    
    # Check if max rounds reached
    if round_number > tournament.rounds:
        return False, f"Tournament complete! Maximum {tournament.rounds} rounds reached."
    
    pairings, bye_player = swiss_pairings_participants(participants,round_number)
    bye_players_list = [bye_player] if bye_player else []
    save_round_pairings(tournament_id, round_number, pairings, bye_players_list)
    return True, f"Round {round_number} generated successfully"

def save_round_results(tournament_id, round_number, form_data):
    tournament = Tournament.query.get(tournament_id)
    pairings, existing_bye_players, _ = get_round_data(tournament_id, round_number)
    
    all_bye_players = list(existing_bye_players) if existing_bye_players else []

    def ensure_list(attr):
        return json.loads(attr) if isinstance(attr, str) else attr

    for match in pairings:
        key = f"winner_{match['white_id']}-{match['black_id']}"
        winner = form_data.get(key)
        
        if not winner:
            continue
        
        # ✅ CHECK: Skip if result was already saved
        if match.get('result') and match.get('result') == winner:
            continue  # Result already saved, don't add points again
        
        # ✅ REVERT PREVIOUS RESULT if changing result
        if match.get('result') and match.get('result') != winner:
            # Remove old points before adding new ones
            white = Participant.query.get(match['white_id'])
            black = Participant.query.get(match['black_id'])
            old_result = match.get('result')
            
            # Revert old scores
            if old_result == "white":
                white.score -= tournament.win_points
                black.score -= tournament.loss_points
            elif old_result == "black":
                black.score -= tournament.win_points
                white.score -= tournament.loss_points
            elif old_result == "draw":
                white.score -= tournament.draw_points
                black.score -= tournament.draw_points
            elif old_result in ["bye_white", "bye_black"]:
                if old_result == "bye_white":
                    white.score -= tournament.win_points
                else:
                    black.score -= tournament.win_points
        
        match['result'] = winner
        
        white = Participant.query.get(match['white_id'])
        black = Participant.query.get(match['black_id'])
        
        # Ensure lists are loaded from JSON strings
        white.last_colors = ensure_list(white.last_colors)
        black.last_colors = ensure_list(black.last_colors)
        white.float_history = ensure_list(white.float_history)
        black.float_history = ensure_list(black.float_history)

        # ✅ NOW ADD NEW SCORES (only once)
        if winner == "white":
            white.score += tournament.win_points
            black.score += tournament.loss_points
            
        elif winner == "black":
            black.score += tournament.win_points
            white.score += tournament.loss_points
            
        elif winner == "draw":
            white.score += tournament.draw_points
            black.score += tournament.draw_points
            
        elif winner == "bye_white":
            white.score += tournament.win_points
            white.bye_count = getattr(white, 'bye_count', 0) + 1  # ✅ Increment
            match['result'] = "bye_white"
            if white not in all_bye_players:
                all_bye_players.append(white)
            
        elif winner == "bye_black":
            black.score += tournament.win_points
            black.bye_count = getattr(black, 'bye_count', 0) + 1  # ✅ Increment
            match['result'] = "bye_black"
            if black not in all_bye_players:
                all_bye_players.append(black)


        # Convert lists back to JSON strings for DB
        white.last_colors = json.dumps(white.last_colors)
        black.last_colors = json.dumps(black.last_colors)
        white.float_history = json.dumps(white.float_history)
        black.float_history = json.dumps(black.float_history)
    
    db.session.commit()
    
    # Save all bye players
    save_round_pairings(tournament_id, round_number, pairings, all_bye_players)
    
# ------------------- Routes -------------------
@app.route('/api/tournament/<tname>/debug')
def debug_scores(tname):
    tournament = Tournament.query.filter_by(name=tname).first()
    if not tournament:
        return jsonify({'error': 'Not found'}), 404
    
    participants = Participant.query.filter_by(tournament_id=tournament.id).all()
    data = []
    for p in participants:
        data.append({
            'name': p.name,
            'score': p.score,
            'white_count': p.white_count,
            'black_count': p.black_count,
            'opponents': json.loads(p.opponents) if p.opponents else []
        })
    
    # Sort by score descending
    data.sort(key=lambda x: -x['score'])
    return jsonify(data)

@app.route("/", methods=["GET", "POST"])
def index():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if username == "Admin" and password == "admin123":
            session["username"] = username
            return redirect(url_for("setupdashboard"))
        else:
            error = "Invalid username or password!"
    return render_template("index.html", error=error)

@app.route("/setupdashboard")
def setupdashboard():
    if "username" not in session:
        return redirect(url_for("index"))
    return render_template("dashboard.html")

@app.route("/setuptournament", methods=["GET", "POST"])
def setuptournament():
    if "username" not in session:
        return redirect(url_for("index"))
    
    error = None
    
    if request.method == "POST":
        name = request.form.get("tournament_name", "").strip()
        try:
            rounds = int(request.form.get("rounds"))
            players = int(request.form.get("players"))
            win_points = float(request.form.get("win_points"))
            draw_points = float(request.form.get("draw_points"))
            loss_points = float(request.form.get("loss_points"))
            
            if players % 2 != 0:
                error = "Enter an even number of players"
                return render_template("setuptournament.html", error=error)
            
            if Tournament.query.filter_by(name=name).first():
                error = "Tournament already exists!"
                return render_template("setuptournament.html", error=error)
            
            new_t = Tournament(
                name=name,
                rounds=rounds,
                max_players=players,
                win_points=win_points,
                draw_points=draw_points,
                loss_points=loss_points
            )
            db.session.add(new_t)
            db.session.commit()
            
            return redirect(url_for("setupdashboard"))
        
        except ValueError:
            error = "Invalid input. Check numbers."
    
    return render_template("setuptournament.html", error=error)

@app.route("/api/tournaments")
def api_tournaments():
    tournaments = Tournament.query.all()
    data = []
    
    for t in tournaments:
        player_count = Participant.query.filter_by(tournament_id=t.id).count()
        data.append({
            "id" : t.id,
            "name": t.name,
            "rounds": t.rounds,
            "players": t.max_players,
            "win_points": t.win_points,
            "draw_points": t.draw_points,
            "loss_points": t.loss_points
        })
    
    return jsonify({"tournaments": data})

@app.route('/api/tournament/<tname>/participants', methods=['POST'])
def save_participants(tname):
    tournament = Tournament.query.filter_by(name=tname).first()
    if not tournament:
        return jsonify({'error': 'Tournament not found'}), 404
    
    data = request.get_json(force=True) or []
    
    if not isinstance(data, list):
        return jsonify({'error': 'Invalid data format'}), 400
    
    Participant.query.filter_by(tournament_id=tournament.id).delete()
    db.session.commit()
    
    for p in data:
        name = p.get('name')
        elo = p.get('elo', 1000)
        if not name:
            continue
        try:
            elo = int(elo)
        except ValueError:
            elo = 1000
        
        db.session.add(Participant(
            name=name,
            elo=elo,
            tournament_id=tournament.id,
            score=0.0,
            opponents="[]",
            white_count=0,
            black_count=0
        ))
    
    db.session.commit()
    return jsonify({"status": "ok"})

@app.route("/api/tournament/<int:tournament_id>/rounds", methods=["GET"])
def api_tournament_rounds(tournament_id):
    """Get all rounds for a specific tournament"""
    rounds = Round.query.filter_by(tournament_id=tournament_id).order_by(Round.round_number).all()
    
    rounds_data = []
    for rnd in rounds:
        pairings, bye_players, results = get_round_data(tournament_id, rnd.round_number)

        bye_names = ', '.join([p.name for p in bye_players]) if bye_players else None
        
        rounds_data.append({
            'round_number': rnd.round_number,
            'pairings': pairings,
            'bye_player': bye_names,
            'results': results
        })
    
    return jsonify({
        'status': 'ok',
        'rounds': rounds_data
    })

@app.route('/api/tournament/<tname>/color-debug')
def color_debug(tname):
    tournament = Tournament.query.filter_by(name=tname).first()
    if not tournament:
        return jsonify({'error': 'Not found'}), 404
    
    participants = Participant.query.filter_by(tournament_id=tournament.id).all()
    data = []
    for p in participants:
        last_colors = json.loads(p.last_colors) if p.last_colors else []
        data.append({
            'name': p.name,
            'score': p.score,
            'white_count': p.white_count,
            'black_count': p.black_count,
            'color_diff': p.white_count - p.black_count,
            'last_colors': last_colors,
            'last_two': last_colors[-2:] if len(last_colors) >= 2 else last_colors,
            'next_preference': get_color_preference(p, get_current_round_number(tournament.id) + 1)
        })
    
    data.sort(key=lambda x: -x['score'])
    return jsonify(data)

@app.route("/rounds", methods=["GET", "POST"])
def rounds():
    if "username" not in session:
        return redirect(url_for("index"))
    
    tournaments = Tournament.query.all()
    selected_tournament = None
    rounds_data = []
    error_message = None
    success_message = None
    
    # ⭐ AUTO-SELECT TOURNAMENT FROM URL PARAMETER
    tournament_id_param = request.args.get('tournament_id', type=int)
    if tournament_id_param:
        selected_tournament = Tournament.query.get(tournament_id_param)
        if selected_tournament:
            rounds_data = load_rounds(selected_tournament.id)
    
    if request.method == "POST":
        action = request.form.get("action")
        tournament_id = request.form.get("tournament_id")
        
        if tournament_id:
            selected_tournament = Tournament.query.get(int(tournament_id))
        
        if action == "load_tournament" and selected_tournament:
            rounds_data = load_rounds(selected_tournament.id)
        
        elif action == "save_results" and selected_tournament:
            round_number = int(request.form.get("round_number"))
            save_round_results(selected_tournament.id, round_number, request.form)
            rounds_data = load_rounds(selected_tournament.id)
            success_message = f"Round {round_number} results saved successfully!"
        
        elif action == "generate_next_round" and selected_tournament:
            success, message = generate_next_round(selected_tournament.id)
            if success:
                success_message = message
            else:
                error_message = message
            rounds_data = load_rounds(selected_tournament.id)
    
    return render_template("rounds.html",
                         tournaments=tournaments,
                         selected_tournament=selected_tournament,
                         rounds_data=rounds_data,
                         error_message=error_message,
                         success_message=success_message)

@app.route('/api/tournament/<tname>/participant-count')
def get_participant_count(tname):
    tournament = Tournament.query.filter_by(name=tname).first()
    if not tournament:
        return jsonify({'count': 0})
    
    count = Participant.query.filter_by(tournament_id=tournament.id).count()
    return jsonify({'count': count})

@app.route('/api/tournament/<tname>/participants', methods=['GET'])
def get_participants(tname):
    tournament = Tournament.query.filter_by(name=tname).first()
    if not tournament:
        return jsonify({'error': 'Tournament not found'}), 404
    
    participants = Participant.query.filter_by(tournament_id=tournament.id).all()
    data = [{'name': p.name, 'elo': p.elo} for p in participants]
    
    return jsonify({'participants': data})


@app.route('/api/tournament/<tname>/standings')
def get_standings(tname):
    tournament = Tournament.query.filter_by(name=tname).first()
    if not tournament:
        return jsonify({'error': 'Tournament not found'}), 404
    
    participants = Participant.query.filter_by(tournament_id=tournament.id).all()
    
    # Build standings with tiebreakers
    standings = []
    for p in participants:
        opponents_list = json.loads(p.opponents) if p.opponents else []
        
        # Calculate Buchholz (sum of opponents' scores)
        buchholz = 0.0
        opp_scores = []
        for opp_id in opponents_list:
            opp = Participant.query.get(opp_id)
            if opp:
                buchholz += opp.score
                opp_scores.append(opp.score)
        
        # Buchholz Cut-1 (exclude lowest opponent score)
        buchholz_cut1 = sum(sorted(opp_scores)[1:]) if len(opp_scores) > 1 else buchholz
        games_played = p.white_count + p.black_count

        bye_count = getattr(p,'bye_count',0)

        standings.append({
            'id': p.id,
            'name': p.name,
            'elo': p.elo,
            'score': p.score,
            'buchholz': buchholz,
            'buchholz_cut1': buchholz_cut1,
            'games_played': games_played,
            'white_count': p.white_count,
            'black_count': p.black_count,
            'bye_count': bye_count
        })
    
    # Sort: Score → Buchholz Cut-1 → Buchholz → Games as Black
    standings.sort(key=lambda x: (-x['score'], -x['buchholz_cut1'], -x['buchholz'],-x['elo'], -x['black_count'],-x['bye_count']))
    
    for i, player in enumerate(standings):
        player['rank'] = i + 1
    
    return jsonify({
        'tournament': tournament.name,
        'total_rounds': tournament.rounds,
        'current_round': get_current_round_number(tournament.id),
        'standings': standings
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT",3000))
    app.run(host="0.0.0.0",port=port,debug=True)