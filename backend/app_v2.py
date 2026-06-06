"""
Gaokao Volunteer Recommendation System - Backend v2
Complete matching engine per detailed workflow logic.
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3, os, math

app = Flask(__name__)
CORS(app)

DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "gaokao_v4.db")

def get_db():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db

# ── API 1: List provinces ──
@app.route('/api/provinces', methods=['GET'])
def list_provinces():
    db = get_db()
    rows = db.execute(
        "SELECT id, name FROM provinces ORDER BY folder_order"
    ).fetchall()
    db.close()
    return jsonify([{'id': r['id'], 'name': r['name']} for r in rows])

# ── API 2: Main recommendation engine ──
@app.route('/api/recommend', methods=['POST'])
def recommend():
    data = request.json or {}
    province_id = data.get('province_id')
    category = data.get('category', 'physics')
    user_rank = data.get('rank')
    user_score = data.get('score')
    subjects = data.get('subjects', [])  # e.g. ['物理','化学','生物']
    language = data.get('language', '')
    color_blind = data.get('color_blind', False)
    gender = data.get('gender', '')
    preferred_cities = data.get('cities', [])
    preferred_majors = data.get('major_categories', [])
    max_tuition = data.get('max_tuition')
    priority = data.get('priority', 'balanced')  # school_first / balanced / major_first

    db = get_db()

    # ── MODULE 1: Data Prep ──
    # 1a: Rank -> equivalent score via score_distribution
    if user_rank and province_id:
        row = db.execute("""
            SELECT score FROM score_distribution
            WHERE province_id=? AND year=2025 AND category=?
              AND cumulative_count >= ?
            ORDER BY cumulative_count ASC LIMIT 1
        """, (province_id, category, user_rank)).fetchone()
        if not row:
            row = db.execute("""
                SELECT score, cumulative_count FROM score_distribution
                WHERE province_id=? AND year=2025 AND category=?
                ORDER BY ABS(cumulative_count - ?) LIMIT 1
            """, (province_id, category, user_rank)).fetchone()
        eq_score = row['score'] if row else user_score
        same_score_count = db.execute("""
            SELECT section_count FROM score_distribution
            WHERE province_id=? AND year=2025 AND category=? AND score=?
        """, (province_id, category, eq_score)).fetchone()
        same_count = same_score_count['section_count'] if same_score_count else 0
    else:
        eq_score = user_score
        same_count = 0

    # 1b: Get batch lines for line-diff
    batch_lines = db.execute("""
        SELECT batch, score FROM score_lines
        WHERE province_id=? AND year=2025 AND category=?
        ORDER BY score DESC
    """, (province_id, category)).fetchall()

    # Find the relevant undergrad batch line
    ug_batch = None
    for bl in batch_lines:
        bn = str(bl['batch'])
        if '本科' in bn and '提前' not in bn and '特殊' not in bn and '艺' not in bn:
            ug_batch = bl
            break
    if not ug_batch and batch_lines:
        ug_batch = batch_lines[0]

    line_diff = (user_score - ug_batch['score']) if ug_batch and user_score else None
    batch_line_score = ug_batch['score'] if ug_batch else None
    batch_line_name = ug_batch['batch'] if ug_batch else None

    # ── MODULE 2: Core Algorithm ──
    # Query admission history for matching records
    # Exclude vocational (专科) batches
    conditions = ["a.province_id = ?", "a.category = ?", "a.min_rank IS NOT NULL",
                  "a.min_score IS NOT NULL", "a.year >= 2022",
                  "a.batch NOT LIKE '%专科%'", "a.batch NOT LIKE '%高职%'"]
    params = [province_id, category]

    # Hard filter: subject matching
    if subjects and len(subjects) > 0:
        subj_conds = []
        for s in subjects:
            subj_conds.append("a.subject_requirement LIKE ?")
            params.append(f"%{s}%")
        if subj_conds:
            conditions.append(f"({' OR '.join(subj_conds)})")

    # Hard filter: physical exam
    if color_blind:
        conditions.append("""a.school_major_id IN (
            SELECT sm.id FROM school_majors sm
            WHERE sm.physical_exam_req IS NULL
               OR sm.physical_exam_req NOT LIKE '%色盲%'
               OR sm.physical_exam_req NOT LIKE '%色弱%'
        )""")

    # Hard filter: language
    if language:
        conditions.append("""a.school_major_id IN (
            SELECT sm.id FROM school_majors sm
            WHERE sm.language_req IS NULL
               OR sm.language_req LIKE ?
        )""")
        params.append(f"%{language}%")

    # Hard filter: gender
    if gender:
        conditions.append("""a.school_major_id IN (
            SELECT sm.id FROM school_majors sm
            WHERE sm.gender_restrict IS NULL
               OR sm.gender_restrict != ?
        )""")
        opposite = 'female_only' if gender == 'male' else 'male_only'
        params.append(opposite)

    # Build and execute query
    sql = f"""
        SELECT a.id, a.school_id, a.major_id, a.school_major_id,
               a.year, a.batch, a.min_score, a.min_rank, a.subject_requirement,
               s.name as school_name, s.level, s.is_985, s.is_211, s.city, s.school_type,
               m.name as major_name, m.category as major_category,
               sm.single_subject_req, sm.gender_restrict, sm.physical_exam_req,
               sm.language_req, sm.tuition, sm.duration
        FROM admission_history a
        JOIN schools s ON a.school_id = s.id
        JOIN majors m ON a.major_id = m.id
        LEFT JOIN school_majors sm ON a.school_major_id = sm.id
        WHERE {' AND '.join(conditions)}
        ORDER BY a.min_rank ASC
        LIMIT 5000
    """

    rows = db.execute(sql, params).fetchall()

    # Group by (school_id, major_id) to get multi-year stats
    school_major_stats = {}
    for r in rows:
        key = (r['school_id'], r['major_id'])
        if key not in school_major_stats:
            sm = dict(r)
            sm['years'] = []
            sm['all_ranks'] = []
            sm['all_scores'] = []
            school_major_stats[key] = sm
        school_major_stats[key]['years'].append(r['year'])
        school_major_stats[key]['all_ranks'].append(r['min_rank'])
        school_major_stats[key]['all_scores'].append(r['min_score'])

    # ── MODULE 3: Classification & Scoring ──
    results = []
    for (sid, mid), sm in school_major_stats.items():
        # Get the latest (2025 or most recent) rank
        ranks_2025 = [r for r, y in zip(sm['all_ranks'], sm['years']) if y == 2025]
        ranks_2024 = [r for r, y in zip(sm['all_ranks'], sm['years']) if y == 2024]
        latest_ranks = ranks_2025 or ranks_2024 or sm['all_ranks']

        if not latest_ranks:
            continue

        ref_rank = min(latest_ranks)  # best rank achieved
        avg_rank = sum(sm['all_ranks']) / len(sm['all_ranks'])
        avg_score = sum(sm['all_scores']) / len(sm['all_scores'])

        # 冲稳保 classification
        if user_rank:
            pct_diff = (ref_rank - user_rank) / user_rank * 100
            if pct_diff < -10:
                risk_level = 'chong'
                risk_label = '冲刺'
                risk_desc = '可能有难度'
            elif -10 <= pct_diff <= 5:
                risk_level = 'wen'
                risk_label = '稳妥'
                risk_desc = '录取希望较大'
            else:
                risk_level = 'bao'
                risk_label = '保底'
                risk_desc = '录取把握很大'
        else:
            risk_level = 'unknown'
            risk_label = '未知'
            risk_desc = '请提供全省位次'

        # Multi-dimensional scoring
        score_school = 0
        if sm['is_985']: score_school += 30
        if sm['is_211']: score_school += 20
        if sm['level'] == '本科': score_school += 10
        # City weight: user preferred cities get bonus
        city_bonus = 5 if sm['city'] in (preferred_cities or []) else 0
        # Major weight
        major_bonus = 5 if sm['major_category'] in (preferred_majors or []) else 0

        # Tuition filter
        if max_tuition and sm['tuition'] and sm['tuition'] > max_tuition:
            continue

        # Priority adjustment
        if priority == 'school_first':
            total_score = score_school * 2 + city_bonus + major_bonus
        elif priority == 'major_first':
            total_score = score_school + city_bonus + major_bonus * 2
        else:
            total_score = score_school + city_bonus + major_bonus

        results.append({
            'school_id': sid,
            'major_id': mid,
            'school_major_id': sm['school_major_id'],
            'school_name': sm['school_name'],
            'major_name': sm['major_name'],
            'major_category': sm['major_category'],
            'city': sm['city'],
            'school_level': sm['level'],
            'school_type': sm['school_type'],
            'is_985': sm['is_985'],
            'is_211': sm['is_211'],
            'batch': sm['batch'],
            'subject_requirement': sm['subject_requirement'],
            'ref_rank': ref_rank,
            'avg_rank': round(avg_rank),
            'avg_score': round(avg_score),
            'risk_level': risk_level,
            'risk_label': risk_label,
            'risk_desc': risk_desc,
            'years_data': len(sm['years']),
            'all_ranks': sorted(sm['all_ranks']),
            'single_subject_req': sm['single_subject_req'],
            'gender_restrict': sm['gender_restrict'],
            'physical_exam_req': sm['physical_exam_req'],
            'language_req': sm['language_req'],
            'tuition': sm['tuition'],
            'duration': sm['duration'],
            'total_score': total_score,
        })

    # Sort by risk_level then by total_score
    risk_order = {'chong': 0, 'wen': 1, 'bao': 2, 'unknown': 3}
    results.sort(key=lambda x: (risk_order.get(x['risk_level'], 4), -x['total_score']))

    # Group results
    chong_list = [r for r in results if r['risk_level'] == 'chong'][:20]
    wen_list = [r for r in results if r['risk_level'] == 'wen'][:20]
    bao_list = [r for r in results if r['risk_level'] == 'bao'][:20]

    # Global warnings
    total_count = len(chong_list) + len(wen_list) + len(bao_list)
    chong_pct = len(chong_list) / max(total_count, 1) * 100
    warnings = []
    if chong_pct > 50:
        warnings.append(f"冲刺志愿占比{chong_pct:.0f}%，滑档风险较高，建议增加稳妥志愿")
    if len(wen_list) < 3:
        warnings.append("稳妥志愿不足3个，建议补充")
    if len(bao_list) < 2:
        warnings.append("保底志愿不足2个，存在滑档风险")

    db.close()

    return jsonify({
        'success': True,
        'user': {
            'province_id': province_id,
            'category': category,
            'score': user_score,
            'rank': user_rank,
            'eq_score': eq_score,
            'same_rank_count': same_count,
            'line_diff': line_diff,
            'batch_line_score': batch_line_score,
            'batch_line_name': batch_line_name,
        },
        'recommendations': {
            'chong': chong_list,
            'wen': wen_list,
            'bao': bao_list,
        },
        'warnings': warnings,
        'total_matched': len(results),
    })

# ── API 3: School detail ──
@app.route('/api/school-detail', methods=['POST'])
def school_detail():
    data = request.json or {}
    school_id = data.get('school_id')
    major_id = data.get('major_id')
    province_id = data.get('province_id')

    db = get_db()

    # School info
    school = db.execute("SELECT * FROM schools WHERE id=?", (school_id,)).fetchone()
    major = db.execute("SELECT * FROM majors WHERE id=?", (major_id,)).fetchone()
    sm = db.execute("SELECT * FROM school_majors WHERE school_id=? AND major_id=?", (school_id, major_id)).fetchone()

    # Multi-year data
    history = db.execute("""
        SELECT year, batch, min_score, min_rank, enrollment_count, subject_requirement
        FROM admission_history
        WHERE school_id=? AND major_id=? AND province_id=?
        ORDER BY year DESC
    """, (school_id, major_id, province_id)).fetchall()

    # Professional group info
    pg = db.execute("""
        SELECT pg.group_identifier, pg.min_rank, pg.min_score, gms.major_count,
               gms.best_rank, gms.worst_rank
        FROM major_group_mapping mgm
        JOIN professional_groups pg ON mgm.professional_group_id = pg.id
        LEFT JOIN group_major_stats gms ON pg.id = gms.professional_group_id
        WHERE mgm.school_id=? AND mgm.major_id=? AND mgm.province_id=? AND mgm.year=2025
        LIMIT 1
    """, (school_id, major_id, province_id)).fetchone()

    # Majors in same group
    group_majors = []
    if pg:
        group_majors = db.execute("""
            SELECT DISTINCT m.name, m.category
            FROM major_group_mapping mgm
            JOIN majors m ON mgm.major_id = m.id
            WHERE mgm.professional_group_id = (
                SELECT professional_group_id FROM major_group_mapping
                WHERE school_id=? AND major_id=? AND province_id=? AND year=2025 LIMIT 1
            )
        """, (school_id, major_id, province_id)).fetchall()

    db.close()

    result = {
        'school': dict(school) if school else None,
        'major': dict(major) if major else None,
        'school_major': dict(sm) if sm else None,
        'history': [dict(r) for r in history],
        'professional_group': dict(pg) if pg else None,
        'group_majors': [dict(r) for r in group_majors],
    }
    return jsonify({'success': True, 'detail': result})

# ── API 4: Province stats ──
@app.route('/api/province-stats/<int:province_id>/<category>', methods=['GET'])
def province_stats(province_id, category):
    db = get_db()
    row = db.execute("""
        SELECT MAX(cumulative_count) as total_examinees,
               COUNT(*) as score_points
        FROM score_distribution
        WHERE province_id=? AND category=? AND year=2025
    """, (province_id, category)).fetchone()
    lines = db.execute("""
        SELECT batch, score FROM score_lines
        WHERE province_id=? AND category=? AND year=2025
        ORDER BY score DESC
    """, (province_id, category)).fetchall()
    db.close()
    return jsonify({
        'total_examinees': row['total_examinees'] if row else 0,
        'score_points': row['score_points'] if row else 0,
        'batch_lines': [dict(r) for r in lines],
    })

if __name__ == '__main__':
    print("Gaokao Recommendation System v2 starting...")
    print(f"Database: {DB}")
    print(f"DB exists: {os.path.exists(DB)}")
    print(f"DB size: {os.path.getsize(DB)/1024/1024:.0f} MB" if os.path.exists(DB) else "DB NOT FOUND!")
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=False)
