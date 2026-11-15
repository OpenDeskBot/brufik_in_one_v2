"""Generate the reviewed 15-scene vector library; preserve speech poses and aliases."""
import copy
import json
from pathlib import Path

CYAN = 2047
AMBER = 65024
BLACK = 0
# Original cheek colors, positions and ellipse radii from the reviewed baseline.
BLUSH_HAPPY_SMALL = (61, 223, 132, 8, 4, 64064)
BLUSH_HAPPY_LARGE = (58, 220, 130, 13, 6, 64064)
BLUSH_SHY_SMALL = (62, 222, 133, 9, 4, 63488)
BLUSH_SHY_LARGE = (56, 216, 129, 15, 8, 63488)
BLUSH_GREETING = (60, 222, 132, 9, 4, 64064)


def pose(ms, *, eyes=(11, 11), widths=(11, 11), dx=0, dy=0, mouth=(56, 16), my=160, blush=0, brows=0, dots=0, tear=0, sparkle=0, bulb=0, question=False, frown=False):
    """Stable primitive topology allows interpolation and explicitly replaces every layer."""
    mw, mh = mouth
    mx = 176 + dx
    extras = [dict(shape='circle', x=x, y=y, r=0, c=c) for x,y,c in [(65+dx,132+dy,AMBER),(221+dx,132+dy,AMBER),(190+dx,129,CYAN),(125,191,AMBER),(142,191,AMBER),(159,191,AMBER)]]
    extras += [dict(shape='line', x1=a, y1=b, x2=c, y2=d, c=BLACK) for a,b,c,d in [(88+dx,76,117+dx,76),(167+dx,76,196+dx,76),(223,83,223,99),(215,91,231,91)]]
    extras[:2] = [dict(shape='ellipse_fill', x=x, y=132, rw=0, rh=0, c=BLACK) for x in (61, 223)]
    if blush:
        left, right, y, rw, rh, color = blush
        extras[:2] = [dict(shape='ellipse_fill', x=x, y=y, rw=rw, rh=rh, c=color) for x in (left, right)]
    if dots:
        for i in range(3):
            extras[i+3].update(x=125+i*17, y=191, r=3+(i==dots-1)*2, c=AMBER)
    if tear:
        extras[2].update(x=190+dx, y=119+tear, r=3, c=CYAN)
    if brows:
        extras[6].update(x1=88+dx,y1=76-brows,x2=117+dx,y2=76+brows,c=CYAN)
        extras[7].update(x1=167+dx,y1=76+brows,x2=196+dx,y2=76-brows,c=CYAN)
    # Fixed slots for a bulb: halo, glass, socket and three light rays.
    extras += [dict(shape='circle_outline', x=235, y=66, r=15, c=BLACK),
               dict(shape='circle', x=235, y=66, r=9, c=BLACK),
               dict(shape='round_rect', x=230, y=76, w=10, h=5, radius=2, c=BLACK)]
    extras += [dict(shape='line', x1=a, y1=b, x2=c, y2=d, c=BLACK)
               for a,b,c,d in [(235,43,235,48),(214,54,219,57),(251,54,256,51)]]
    if bulb:
        bright = bulb == 2
        extras[10]['c'] = 19040 if bright else 6304
        extras[11]['c'] = 65516 if bright else 44352
        extras[12]['c'] = 65024 if bright else 33792
        for ray in extras[13:16]:
            ray['c'] = 65504 if bright else 19040
        extras[8].update(x1=232,y1=66,x2=235,y2=72,c=33792)
        extras[9].update(x1=238,y1=66,x2=235,y2=72,c=33792)
    if question:
        for slot, coords in zip((8, 9, 13, 14, 15),
                                ((229,57,235,52), (235,52,243,52),
                                 (243,52,248,57), (248,57,248,64), (248,64,239,72))):
            a,b,c,d = coords
            extras[slot].update(x1=a,y1=b,x2=c,y2=d,c=65504)
        extras[10].update(r=0)
        extras[11].update(x=239,y=85,r=2,c=65504)
        extras[12].update(x=237,y=71,w=4,h=7,radius=2,c=65504)
    if sparkle:
        extras[8].update(x1=223,y1=83,x2=223,y2=99,c=AMBER)
        extras[9].update(x1=215,y1=91,x2=231,y2=91,c=AMBER)
    return dict(ms=ms, elements=dict(
        eye_l=[dict(shape='ellipse_fill',x=104+dx,y=97+dy,rw=widths[0],rh=eyes[0],c=CYAN)],
        eye_r=[dict(shape='ellipse_fill',x=180+dx,y=97+dy,rw=widths[1],rh=eyes[1],c=CYAN)],
        nose=[dict(shape='circle',x=0,y=0,r=0,c=BLACK)],
        mouth=[dict(shape='round_rect',x=mx-mw//2,y=my+dy-mh//2,w=mw,h=mh,radius=mh//2,c=CYAN),
               dict(shape='round_rect',x=mx-mw//2+4,y=my+dy-mh//2+(6 if frown else 4),w=max(1,mw-8),h=(mh+8 if frown else max(1,mh-8)),radius=(max(1,mh//2-3) if frown else max(0,(mh-8)//2)),c=BLACK)],
        extra=extras))


def scenes():
    p = pose
    result = {
        'idle': [p(160),p(1700),p(160,dx=4),p(500,dx=4),p(160),p(90,eyes=(1,1)),p(90),p(1900)],
        'happy': [p(140,eyes=(6,6),mouth=(66,24),blush=BLUSH_HAPPY_SMALL),p(150,eyes=(3,3),mouth=(72,30),dy=-3,blush=BLUSH_HAPPY_LARGE),p(160,eyes=(5,5),mouth=(68,26),blush=BLUSH_HAPPY_LARGE),p(650,eyes=(5,5),mouth=(68,26),blush=BLUSH_HAPPY_LARGE),p(300)],
        'shy': [p(220,dx=-5,eyes=(7,7),mouth=(32,12),blush=BLUSH_SHY_SMALL),p(550,dx=-5,eyes=(7,7),mouth=(32,12),blush=BLUSH_SHY_SMALL),p(160,dx=-5,eyes=(3,3),mouth=(32,12),blush=BLUSH_SHY_LARGE),p(400,dx=-5,eyes=(3,3),mouth=(32,12),blush=BLUSH_SHY_LARGE),p(320)],
        'angry': [p(160,eyes=(7,7),brows=5,mouth=(42,12)),p(220,eyes=(5,5),brows=7,mouth=(46,12)),p(650,eyes=(5,5),brows=7,mouth=(46,12)),p(380)],
        'surprised': [p(80,eyes=(7,7)),p(120,eyes=(20,20),widths=(17,17),mouth=(32,38)),p(130,eyes=(17,17),widths=(15,15),mouth=(28,32)),p(550,eyes=(17,17),widths=(15,15),mouth=(28,32)),p(320)],
        'sad': [p(220,eyes=(8,8),brows=-5,mouth=(38,12),dy=3),p(550,eyes=(8,8),brows=-5,mouth=(38,12),dy=3),p(180,eyes=(5,5),brows=-6,mouth=(34,10),dy=4),p(550,eyes=(5,5),brows=-6,mouth=(34,10),dy=4),p(400)],
        'sleep': [p(180,eyes=(3,3),mouth=(32,12)),p(1200,eyes=(2,2),mouth=(32,12)),p(600,eyes=(2,2),mouth=(34,14),dy=2),p(600,eyes=(2,2),mouth=(32,12)),p(1000,eyes=(2,2),mouth=(32,12))],
        'thinking': [p(180,dx=6,dy=-3,eyes=(9,7),mouth=(32,12),dots=1),p(420,dx=6,dy=-3,eyes=(9,7),mouth=(32,12),dots=1),p(420,dx=6,dy=-3,eyes=(9,7),mouth=(32,12),dots=2),p(420,dx=6,dy=-3,eyes=(9,7),mouth=(32,12),dots=3),p(100,dx=6,dy=-3,eyes=(2,2),mouth=(32,12),dots=1),p(120,dx=6,dy=-3,eyes=(9,7),mouth=(32,12),dots=1)],
        'listening': [p(160,eyes=(14,14),mouth=(44,12)),p(800,eyes=(14,14),mouth=(44,12)),p(120,eyes=(12,12),dy=2,mouth=(44,12)),p(160,eyes=(14,14),mouth=(44,12)),p(90,eyes=(2,2),mouth=(44,12)),p(100,eyes=(14,14),mouth=(44,12))],
        'wake': [p(80,eyes=(2,2),mouth=(32,12)),p(140,eyes=(3,9),mouth=(34,14),dx=-2),p(90,eyes=(1,1),mouth=(34,14)),p(220,eyes=(5,5),mouth=(28,36),dy=-4),p(180,eyes=(5,5),mouth=(28,36),dy=-4),p(130,eyes=(16,16),mouth=(52,22),dy=-2),p(90,eyes=(1,1),mouth=(52,22)),p(300,eyes=(6,6),mouth=(62,24)),p(300)],
        'greeting': [p(140,eyes=(7,7),mouth=(64,24),blush=BLUSH_GREETING),p(120,eyes=(2,12),mouth=(64,24),blush=BLUSH_GREETING),p(200,eyes=(2,12),mouth=(64,24),blush=BLUSH_GREETING),p(120,eyes=(7,7),mouth=(64,24),blush=BLUSH_GREETING),p(430,eyes=(7,7),mouth=(64,24),blush=BLUSH_GREETING),p(300)],
        'curious': [p(180,eyes=(8,17),widths=(9,14),dx=4,mouth=(28,18),question=True),p(600,eyes=(8,17),widths=(9,14),dx=4,mouth=(28,18),question=True),p(160,eyes=(10,15),widths=(10,13),dx=2,mouth=(30,18),question=True),p(320)],
        'got_it': [p(100,eyes=(14,14),mouth=(48,22),bulb=1),p(150,eyes=(5,5),mouth=(64,24),dy=2,bulb=2),p(150,eyes=(6,6),mouth=(64,24),bulb=1),p(650,eyes=(6,6),mouth=(64,24),bulb=2),p(350)],
        'oops': [p(100,eyes=(16,16),mouth=(26,28)),p(180,eyes=(5,6),widths=(13,12),brows=-8,mouth=(30,12),tear=3),p(200,eyes=(3,4),widths=(13,12),brows=-10,mouth=(30,12),dy=2,tear=12),p(700,eyes=(3,4),widths=(13,12),brows=-10,mouth=(30,12),dy=2,tear=12),p(380)],
        'wink': [p(120,eyes=(8,12),mouth=(60,20),blush=BLUSH_HAPPY_SMALL),p(90,eyes=(1,12),mouth=(64,22),blush=BLUSH_HAPPY_SMALL),p(220,eyes=(1,12),mouth=(64,22),blush=BLUSH_HAPPY_SMALL),p(120,eyes=(8,12),mouth=(60,20),blush=BLUSH_HAPPY_SMALL),p(250)],
    }

    # Three Z glyphs use line primitives so browser and firmware need no font.
    # Fade while drifting up; invisible endpoints make the loop seamless.
    for frame, drift, brightness in zip(result['sleep'], (0, 3, 9, 16, 21), (0, 1, 2, 1, 0)):
        z_lines = []
        for j, (x, y, size) in enumerate(((209, 100, 8), (228, 78, 11), (250, 51, 14))):
            yy = y - drift
            colors = ((0, 0, 0), (16904, 33840, 2113), (33840, 50712, 33840))
            color = colors[brightness][j]
            for a,b,c,d in ((x,yy,x+size,yy), (x+size,yy,x,yy+size), (x,yy+size,x+size,yy+size)):
                z_lines.append(dict(shape='line', x1=a, y1=b, x2=c, y2=d, c=color))
        frame['elements']['extra'][6:15] = z_lines
    return result


def refine(doc):
    result = copy.deepcopy(doc)
    revised = scenes()
    # Keep the former waiting IDs as aliases for existing scene calls.
    result['emotions'] = [e for e in result['emotions'] if e['name'] != 'waiting']
    assert {e['name'] for e in result['emotions']} == revised.keys()
    for e in result['emotions']:
        e['frames'] = revised[e['name']]
        if e['name'] == 'thinking':
            e['alias'] = list(dict.fromkeys(e.get('alias', []) + ['waiting', 'loading', 'busy']))
    return result


if __name__ == '__main__':
    path = Path(__file__).resolve().parents[1] / 'data/global/deskbot-face.json'
    doc = json.loads(path.read_text())
    path.write_text(json.dumps(refine(doc),ensure_ascii=False,indent=2)+'\n')
