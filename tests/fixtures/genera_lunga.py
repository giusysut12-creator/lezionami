"""Genera tests/fixtures/lunga.txt: trascrizione fittizia lunga (circa 2 ore e mezza di parlato).

Ogni modulo ha numeri e condizioni distinti; l'ultimo modulo contiene informazioni
che compaiono solo alla fine, per verificare che la segmentazione non perda la parte finale.
"""

from pathlib import Path

MODULES = [
    ("adesione", "Si può aderire da 18 a 70 anni. Il contributo minimo è di 600 euro l'anno, il massimo deducibile fiscalmente è 5.164,57 euro l'anno."),
    ("comparti", "Ci sono tre comparti: Garantito, Bilanciato e Dinamico. Il comparto Garantito restituisce almeno i contributi versati al pensionamento, non in caso di trasferimento."),
    ("costi di ingresso", "La quota di iscrizione è di 25 euro una tantum. Su ogni contributo si paga un caricamento dell'1%."),
    ("costi annui", "Le commissioni annue sono dello 0,9% per il Garantito, dell'1,1% per il Bilanciato e dell'1,3% per il Dinamico, calcolate sul patrimonio del comparto."),
    ("rendimenti storici", "Nel quinquennio 2021-2025 il Bilanciato ha reso in media il 2,8% annuo netto. È un dato passato e non garantisce i rendimenti futuri."),
    ("fiscalità dei contributi", "I contributi sono deducibili dal reddito fino a 5.164,57 euro. È una deduzione, non un'esenzione: le prestazioni saranno tassate."),
    ("tassazione delle prestazioni", "La prestazione è tassata al 15%, che scende di 0,3 punti per ogni anno oltre il quindicesimo di partecipazione, fino a un minimo del 9%."),
    ("anticipazioni", "Dopo 8 anni si può chiedere un'anticipazione fino al 75% per l'acquisto della prima casa. Per spese sanitarie gravi l'anticipazione è possibile in ogni momento, sempre fino al 75%."),
    ("riscatti", "Il riscatto totale è possibile per perdita dei requisiti, con tassazione del 23% se non dipende da cause previste dalla legge."),
    ("rendita", "Al pensionamento si può prendere fino al 50% in capitale e il resto come rendita. Il coefficiente di conversione dipende dall'età e dalle tavole in vigore in quel momento."),
    ("switch", "Il cambio di comparto è consentito una volta l'anno, dopo almeno 12 mesi di permanenza, ed è gratuito."),
    ("beneficiari", "In caso di decesso prima del pensionamento la posizione va ai beneficiari designati o, in mancanza, agli eredi."),
]

FINAL = (
    "Ultimo argomento, importante e che compare solo qui: il trasferimento della posizione ad altra forma pensionistica "
    "è possibile dopo 2 anni di partecipazione e costa 50 euro. Domanda dal pubblico: «Il trasferimento fa perdere l'anzianità?» "
    "Risposta: no, l'anzianità maturata viene conservata ai fini fiscali. Grazie a tutti, chiudiamo qui."
)

FILLER = [
    "Eh, allora, come dicevo prima, questo è un punto su cui i clienti fanno spesso domande, quindi vale la pena ripeterlo con calma.",
    "Vedo che qualcuno in chat chiede se le slide saranno disponibili: sì, le trovate poi nell'area riservata, ci pensa la segreteria.",
    "Diciamo che, ecco, il concetto è abbastanza semplice ma va spiegato bene, perché altrimenti si creano aspettative sbagliate.",
    "Faccio un esempio pratico, così ci capiamo: pensate a un cliente di quarant'anni che ha un reddito stabile e vuole integrare la pensione.",
    "Non so se si sente bene, mi fate un cenno in chat? Ok, perfetto, andiamo avanti.",
    "Questo lo ripeto perché è importante: bisogna sempre consegnare la documentazione ufficiale e verificare l'adeguatezza.",
]


def _ts(seconds: int) -> str:
    return f"{seconds // 3600:01d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def build() -> str:
    parts = ["Trascrizione di prova (testo fittizio creato per i test dell'applicazione)", ""]
    seconds = 0
    for index, (topic, fact) in enumerate(MODULES, start=1):
        parts.append(f"{_ts(seconds)} Relatore: Passiamo al modulo {index}, dedicato a {topic}. {fact}")
        for round_ in range(38):
            filler = FILLER[(index + round_) % len(FILLER)]
            seconds += 20
            parts.append(f"{_ts(seconds)} Relatore: {filler} Riprendo il punto su {topic}: {fact}")
        seconds += 20
        parts.append("")
    parts.append(f"{_ts(seconds)} Relatore: {FINAL}")
    return "\n".join(parts) + "\n"


if __name__ == "__main__":
    target = Path(__file__).with_name("lunga.txt")
    target.write_text(build(), encoding="utf-8")
    print(f"Scritto {target} ({len(build()):,} caratteri)")
