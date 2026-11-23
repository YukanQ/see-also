#!/usr/bin/env python3
"""make_sample_library.py - builds the small sample library in examples/sample-library/.

The PDFs are already in the repository; this script only shows where they come
from and lets you rebuild them. All texts were written for this sample and may
be used freely (MIT, like the rest of the repository).

Needs reportlab and Pillow (not needed for see_also itself):
    python3 -m pip install reportlab pillow
    python3 examples/make_sample_library.py

The library has three languages (English, German, French), several areas
(first folder level) and one scanned page without a text layer, so that the
search across languages, the search scope and the OCR run can all be tried.
"""
import os
import sys

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

ZIEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample-library")

DOKUMENTE = {
    "Analog computing/Integrators and feedback.pdf": ("Integrators and feedback", "en", [
        ["An analog computer does not count. It lets a voltage behave like the quantity it stands for: a "
         "falling body, a swinging pendulum, the charge in a capacitor. The machine is set up so that the "
         "same equations hold for the voltages as for the real system, and then it simply runs.",
         "The central element is the integrator. An operational amplifier with a capacitor in its feedback "
         "path adds up its input over time. If the input is an acceleration, the output is a velocity; a "
         "second integrator turns the velocity into a position."],
        ["Feedback closes the loop. The position is fed back, with a negative sign, to the input of the "
         "first integrator. The result is a harmonic oscillator: the voltages swing back and forth like a "
         "mass on a spring, and the frequency is set by two potentiometers.",
         "Because everything happens at once and in parallel, an analog computer solves differential "
         "equations in real time. Its accuracy is limited by its components, typically to a few parts in a "
         "thousand, which was good enough for flight simulators and process control."],
        ["Scaling is the craft of analog programming. Every variable must be mapped to a voltage range so "
         "that no amplifier saturates and no signal drowns in noise. A careful programmer estimates the "
         "largest value each quantity can reach before the machine is switched on.",
         "Today the same ideas live on in control engineering. A thermostat, a cruise control and the "
         "attitude control of a drone all integrate an error and feed it back."]]),
    "Analog computing/Der Differentialanalysator.pdf": ("Der Differentialanalysator", "de", [
        ["Der Differentialanalysator war ein mechanischer Rechner, der Differentialgleichungen durch "
         "Integration löste. Wellen, Zahnräder und Reibradgetriebe übernahmen die Rolle, die später "
         "elektronische Verstärker spielten.",
         "Das Herzstück ist der Reibradintegrator: Ein kleines Rad läuft auf einer drehenden Scheibe. Je "
         "weiter es vom Mittelpunkt entfernt ist, desto schneller dreht es sich. Die Umdrehungen des Rades "
         "summieren so das Produkt aus Abstand und Drehwinkel der Scheibe."],
        ["Um eine Gleichung zu lösen, wurden die Wellen der Maschine von Hand miteinander verbunden. Das "
         "Programm bestand also aus der Verkabelung, genauer: aus der Verbindung mechanischer Achsen. Eine "
         "Umrüstung dauerte oft einen ganzen Tag.",
         "Mit Rückkopplung zwischen den Integratoren ließen sich Schwingungen, Flugbahnen und Wärmeleitung "
         "berechnen. Die Genauigkeit lag bei etwa einem Promille, mehr gab die Mechanik nicht her."]]),
    "Systems/Feedback in everyday machines.pdf": ("Feedback in everyday machines", "en", [
        ["A machine with feedback compares what it does with what it should do, and corrects itself. The "
         "flyball governor on a steam engine is the classic example: when the engine runs too fast, the "
         "balls fly outward and close the steam valve a little.",
         "Negative feedback keeps a system near a set point. It makes a machine stable against "
         "disturbances it was never told about: a heavier load, a colder room, a gust of wind."],
        ["Positive feedback does the opposite. A small deviation is amplified until something limits it. "
         "The howl of a microphone held too close to its loudspeaker is positive feedback; so is the "
         "snap of a light switch, which is built to jump rather than to hover.",
         "Delays make feedback difficult. If the correction arrives too late, the system overshoots, "
         "corrects again, and starts to oscillate. Anyone who has adjusted the hot water in an unfamiliar "
         "shower knows the effect."]]),
    "Systems/Rückkopplung und Gleichgewicht.pdf": ("Rückkopplung und Gleichgewicht", "de", [
        ["Ein Gleichgewicht ist kein Stillstand. Ein Fahrradfahrer bleibt aufrecht, weil er ständig kleine "
         "Lenkbewegungen macht. Er misst die Abweichung und gleicht sie aus, bevor sie groß wird.",
         "So arbeitet jede Regelung: Messen, vergleichen, eingreifen. Der Regler muss die Ursache der "
         "Störung nicht kennen; es genügt, dass er ihre Wirkung bemerkt."],
        ["Schwierig wird es, wenn die Wirkung eines Eingriffs erst spät sichtbar wird. Dann greift der "
         "Regler zu stark ein, und das System schwingt. In der Heizungstechnik hilft man sich mit "
         "Vorhersage: Die Außentemperatur verrät, wie viel Wärme das Haus bald brauchen wird.",
         "Lebewesen regeln Körpertemperatur, Blutzucker und Atmung auf dieselbe Weise. Die Biologie nennt "
         "das Homöostase."]]),
    "Garden/Le compost au jardin.pdf": ("Le compost au jardin", "fr", [
        ["Le compost transforme les déchets de cuisine et du jardin en un engrais doux pour le sol. Il "
         "faut un mélange de matières vertes, riches en azote, et de matières brunes, riches en carbone, "
         "comme les feuilles mortes ou la paille.",
         "Un tas de compost a besoin d'air et d'humidité. Trop sec, il ne se décompose pas ; trop mouillé, "
         "il sent mauvais. On le retourne de temps en temps pour l'aérer."],
        ["Les vers de terre et les micro-organismes font l'essentiel du travail. En été, le cœur du tas "
         "peut dépasser soixante degrés, ce qui détruit une partie des graines de mauvaises herbes.",
         "Après six mois à un an, le compost est mûr : sombre, friable, il sent la terre de forêt. On "
         "l'étale en couche fine au pied des plantes ou on le mélange à la terre des semis."]]),
    "Garden/Soil and water.pdf": ("Soil and water", "en", [
        ["A good garden soil holds water like a sponge and still lets air in. Sand drains quickly and dries "
         "out; clay holds water but becomes hard and airless. Humus, the dark remains of plants, improves "
         "both.",
         "Watering deeply and seldom trains roots to grow down. Frequent light watering keeps them near the "
         "surface, where the soil dries first."],
        ["Mulch, a layer of straw, leaves or compost on the surface, slows evaporation and keeps the soil "
         "cool in summer. It also feeds the worms, which in turn open channels for air and water.",
         "The simplest test of soil moisture is a finger: if the soil is dry two knuckles deep, it is time "
         "to water."]]),
    "Sky/Phases of the moon.pdf": ("Phases of the moon", "en", [
        ["The moon does not shine by itself. We see the half that is lit by the sun, and how much of that "
         "half faces us changes over a month: new moon, waxing crescent, first quarter, full moon and back.",
         "A full cycle of phases takes about twenty-nine and a half days. This is slightly longer than one "
         "orbit around the earth, because the earth moves around the sun in the meantime."],
        ["The moon always turns the same face towards us. Its rotation has been slowed by the earth's "
         "gravity until one turn takes exactly as long as one orbit.",
         "The rhythm of the phases gave many calendars their months. The word month itself is related to "
         "moon."]]),
    "Sky/Les marées.pdf": ("Les marées", "fr", [
        ["Les marées viennent surtout de l'attraction de la Lune. Elle tire un peu plus fort sur la mer du "
         "côté qui lui fait face que sur le centre de la Terre, et un peu moins fort sur le côté opposé. "
         "Il en résulte deux bourrelets d'eau.",
         "Comme la Terre tourne sous ces bourrelets, la plupart des côtes connaissent deux marées hautes "
         "par jour, à environ douze heures et vingt-cinq minutes d'intervalle."],
        ["Le Soleil agit aussi, avec moins de force. Quand il est aligné avec la Lune, à la nouvelle lune "
         "et à la pleine lune, les marées sont les plus fortes : ce sont les vives-eaux. Aux quartiers, "
         "on parle de mortes-eaux.",
         "En Bretagne et dans la baie du Mont-Saint-Michel, la forme des côtes amplifie le phénomène ; "
         "l'écart entre marée haute et marée basse y dépasse dix mètres."]]),
}

SCAN = ("Scans/Note on a pendulum clock (scan).pdf", [
    "A pendulum clock keeps time because a pendulum of a",
    "given length always swings with nearly the same period,",
    "whether the swing is wide or narrow. The escapement",
    "gives the pendulum a small push at each swing and,",
    "in return, lets the gear train advance by one tooth.",
    "",
    "Warmth makes the rod longer and the clock slower.",
    "Good clocks use rods of two metals or of wood to",
    "keep the length constant.",
])


def pdf_mit_text(rel, titel, sprache, seiten):
    pfad = os.path.join(ZIEL, rel)
    os.makedirs(os.path.dirname(pfad), exist_ok=True)
    stile = getSampleStyleSheet()
    teile = [Paragraph(titel, stile["Title"]), Spacer(1, 12)]
    for i, absaetze in enumerate(seiten):
        if i:
            teile.append(PageBreak())
        for a in absaetze:
            teile += [Paragraph(a, stile["BodyText"]), Spacer(1, 8)]
    doc = SimpleDocTemplate(pfad, pagesize=A4, title=titel, author="see_also sample library",
                            lang=sprache, invariant=1)
    doc.build(teile)


def pdf_als_scan(rel, zeilen):
    """Eine Seite als Bild, ohne Textschicht - wie ein Scan."""
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.pdfgen import canvas
    pfad = os.path.join(ZIEL, rel)
    os.makedirs(os.path.dirname(pfad), exist_ok=True)
    bild = Image.new("L", (1654, 2339), 250)          # A4 bei 200 dpi
    zeichnen = ImageDraw.Draw(bild)
    schrift = None
    for f in ("/System/Library/Fonts/Supplemental/Times New Roman.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"):
        if os.path.exists(f):
            schrift = ImageFont.truetype(f, 44)
            break
    schrift = schrift or ImageFont.load_default(size=44)
    y = 260
    for z in zeilen:
        zeichnen.text((170, y), z, fill=25, font=schrift)
        y += 72
    png = pfad + ".png"
    bild.save(png)
    c = canvas.Canvas(pfad, pagesize=A4, invariant=1)
    c.drawImage(png, 0, 0, width=A4[0], height=A4[1])
    c.showPage()
    c.save()
    os.remove(png)


def main():
    for rel, (titel, sprache, seiten) in DOKUMENTE.items():
        pdf_mit_text(rel, titel, sprache, seiten)
    pdf_als_scan(*SCAN)
    print(f"{len(DOKUMENTE) + 1} PDFs in {ZIEL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
