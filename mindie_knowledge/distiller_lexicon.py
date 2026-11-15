"""Lexical categories and morphology for local technical-English clauses.

[Role]
Lexical categories and morphology for local technical-English clauses.
[Invariants]
Case-insensitive lexical categories are independent of normalization rules.
[Timeouts & Locks]
Pure bounded string processing; no I/O, locks, waits, or subprocesses.
[Side-Effect Policy]
No mutations of inputs, files, environment, or external state.
"""

from __future__ import annotations

from contextvars import ContextVar
import re

_PARSE_CACHE: ContextVar[dict | None] = ContextVar("distiller_parse_cache", default=None)

_LEGACY_MENTION = re.compile(
    r"ge_graph|torchair|GE\s*图|aclgraph|torch\.npu\.npu_fusion_attention|"
    r"npu_create_tagged_stream|Notify_Wait|limit_core_num", re.I)
# Soft wraps stay in the clause; paragraphs and explicit list items do not.
_HARD_BREAK = r"\r?\n[ \t]*\r?\n|\r?\n(?=[ \t]*(?:[-*+•]|\d+[.)])[ \t]+)"
_SENTENCE_BOUNDARY = re.compile(r"\.(?!\w)|[。!?！？;；]|" + _HARD_BREAK)
_PREDICATE_WORD = (
    r"(?:is|was|are|were|has|have|had|can|could|will|would|may|might|must|"
    r"should|cannot|not|does|do|did|remains?|fails?|unsupported|unavailable|incompatible|"
    r"forbidden|prohibited|deprecated|exclusive|limited|restricted)\b")
_PREDICATE_ONLY = re.compile(_PREDICATE_WORD + r"\Z", re.I)
# Adverbs precede either a predicate fragment or an independent subject NP.
_ADVERB_WORD = (
    r"(?:no\s+longer|never|only|always|still|not|rarely|seldom|ever|already|also|now|then|"
    r"soon|yet|again|once|often|sometimes|perhaps|maybe|otherwise|instead|meanwhile|moreover|"
    r"furthermore|nevertheless|nonetheless|therefore|thus|hence|however|indeed|quite|rather|"
    r"very|too|so|almost|even|just|much|more|most|less|least|fast|hard|well|far|late|long|early)")
# Positive manner morphology keeps nominal -ly endings available as heads.
_MANNER_LY = (
    r"(?:[a-z]+(?:ical|al|ous|ful|less|ive|able|ible|ant|ent|ate|ari|ori|ing)|"
    r"[a-z]+(?:ab|ib)|(?:un|in|non|over|under|extra|surprising)*"
    r"(?:real|brief|clean|clear|quick|quiet|slow|safe|free|pure|sole|mere|rare|usual|"
    r"normal|near|nearer|hard|soft|deep|high|low|large|wide|narrow|tight|loose|"
    r"direct|indirect|remote|ready|readi|easi|ear|late|long|short|sharp|smooth|"
    r"strict|steep|cheap|crisp|damp|limp|plump|sleepi|wise|unwise|tru|du|full|"
    r"fair|frank|final|general|total|partial|particular|special|complete|entire|"
    r"extreme|simple|simp|amp|triple|trip|outward|inward|name|only|"
    r"part|sudden|happi|ful|explicit|precise|rapid|regular|"
    r"admitted|repeated|seeming|surprising|expected|unexpected))ly")
_LEADING_ADVERBS = re.compile(r"(?P<word>" + _ADVERB_WORD + r"|" + _MANNER_LY + r")\b", re.I)
# Only lexical bases and productive prefixes license -ly verbs; deadjectival
# 'really'/'unreally' (real + -ly) and arbitrary -ply adverbs stay modifiers.
_LY_PREFIX = r"(?!(?:un)?really\Z)(?:re|mis|over|under|pre|post|out|non|un|co|counter|dis|de|super|sub|inter)*"
_LY_STEM = re.compile(
    _LY_PREFIX + r"(?:apply|supply|reply|multiply|rely|comply|imply|fly|ally|bully|rally|tally|dally|sally|ply)", re.I)
_LY_VERB_FORM = re.compile(
    _LY_PREFIX + r"(?:appl|suppl|repl|multipl|rel|compl|impl|fl|all|bull|rall|tall|dall|sall|pl)(?:y|ies|ied)\Z", re.I)
_FUNCTION_WORD = re.compile(
    r"(?:in|on|at|of|for|from|to|with|without|by|via|as|into|onto|until|using|and|or|nor|yet|so|"
    r"upon|through|over|under|across|within|during|behind|before|after|between|among|"
    r"around|against|but|than|that|which|whose|where|when|while|if|unless|because|"
    r"whenever|wherever|whether|once|though|although|whilst)\Z", re.I)
_PREPOSITION = re.compile(
    r"(?:on|with|to|into|onto|until|in|for|from|by|at|upon|via|through|over|under|across|"
    r"within|without|of|during|behind|before|after|between|among|around|against)\b", re.I)
_SUBORDINATOR = re.compile(
    r"(?!as\s+well\s+as\b)(?:as|if|when|whenever|while|whilst|once|though|although|"
    r"even\s+if|even\s+though|unless|where|wherever|whether)\b", re.I)
_DETERMINER = re.compile(r"(?:the|a|an|this|these|that|those|each|every|my|your|his|her|our|their|its|whose|both|many|several|few|any|some|no|all|enough)\Z", re.I)
_PLURAL_DETERMINER = re.compile(r"(?:these|those|both|many|several|few)\Z", re.I)
_SINGULAR_DETERMINER = re.compile(r"(?:a|an|this|that|each|every)\Z", re.I)
_PRONOUN = re.compile(r"(?:we|you|they|it|he|she)\Z", re.I)
_PLURAL_PRONOUN = re.compile(r"(?:we|you|they)\Z", re.I)
_AUXILIARY = re.compile(
    r"(?:is|are|was|were|has|have|had|can|could|will|would|may|might|must|should|cannot|do|does|did)\Z", re.I)
_SINGULAR_AUX = re.compile(r"(?:is|was|has|does)\Z", re.I)
_PLURAL_AUX = re.compile(r"(?:are|were|have|do)\Z", re.I)
_PHRASE_TOKEN = re.compile(r"[a-z0-9]+(?:[-/×][a-z0-9]+)*(?!\w|\.\w)", re.I)
# Distinct past participles never head a finite clause without an auxiliary;
# distinct finite preterites never head a noun phrase or reduced modifier.
_NONFINITE_PAST = re.compile(
    r"(?:re|mis|over|under|out|un|with|fore|up|down|inter|pre|post|co|counter|sub|super|de|dis|auto)*"
    r"(?:given|forgiven|taken|mistaken|undertaken|overtaken|partaken|betaken|forsaken|shaken|"
    r"chosen|frozen|spoken|bespoken|broken|woken|awoken|stolen|woven|interwoven|cloven|proven|disproven|"
    r"written|bitten|smitten|ridden|overridden|hidden|forbidden|bidden|stridden|chidden|slidden|trodden|"
    r"driven|striven|thriven|riven|shriven|risen|arisen|fallen|befallen|swollen|"
    r"eaten|beaten|gotten|forgotten|begotten|stricken|shrunken|drunken|sunken|"
    r"been|seen|foreseen|overseen|done|undone|gone|undergone|foregone|forgone|bygone|"
    r"known|shown|grown|thrown|blown|flown|drawn|withdrawn|mown|sown|sewn|hewn|strewn|sawn|"
    r"worn|sworn|forsworn|torn|borne|forborne|born|shorn|slain|lain|"
    r"sung|rung|sprung|begun|swum|drunk|sunk|shrunk)\b", re.I)
_FINITE_PRETERITE = re.compile(
    r"(?:re|mis|over|under|out|un|with|fore|up|down|inter|pre|post|sub|de|dis)*"
    r"(?:rang|sang|sprang|sank|shrank|drank|began|swam|ran|drove|wrote|rode|strode|rose|arose|"
    r"chose|froze|stole|spoke|broke|woke|awoke|wove|gave|forgave|came|became|went|took|shook|"
    r"forsook|fell|befell|ate|flew|grew|knew|threw|blew|drew|slew|wore|swore|tore|bore|lay|bit|hid|slid|trod)\b", re.I)
_REGULAR_ED = re.compile(
    r"(?!(?:re|over|under|out|un|mis|sub|super|pro|ex|suc)*"
    r"(?:need|ceed|feed|speed|bleed|breed|seed|heed|weed|deed|reed|shred|bed|shed|sled|thread|"
    r"hundred|kindred|hatred)s?\b)[a-z]{2,}ed\b", re.I)
_SYNCRETIC_PARTICIPLE = re.compile(
    r"(?:re|mis|over|under|out|un|with|up|in|pre|post|co|sub|de|dis|broad|fore|multi)*"
    r"(?:sent|spent|lent|bent|meant|dealt|felt|kept|slept|swept|wept|crept|left|lost|cost|"
    r"cast|forecast|put|set|reset|cut|shut|hit|split|burst|thrust|hurt|let|met|bet|quit|rid|"
    r"shed|spread|read|led|fed|bled|bred|sped|fled|held|withheld|upheld|told|sold|paid|laid|"
    r"said|made|built|rebuilt|bought|brought|caught|taught|sought|fought|thought|"
    r"bound|unbound|found|ground|wound|rewound|unwound|stood|understood|withstood|"
    r"sat|shot|won|spun|hung|clung|flung|slung|stung|strung|swung|wrung|dug|stuck|unstuck|struck)\b", re.I)
_FINITE_ING = re.compile(
    r"(?:re|un|over|out|mis|pre|sub)*(?:bring|ring|sing|spring|cling|fling|sling|sting|string|swing|wring|ping)\b", re.I)
_PRESENT_PARTICIPLE = re.compile(
    r"(?!during\b|(?:some|any|no|every)thing\b)[a-z]*[aeiouy][a-z]*ing\b", re.I)
_PREDICATIVE_PARTICIPLE = re.compile(
    r"(?:re|un|mis|over|under|fore|dis)*(?:proven|shown|gone|been|grown|fallen|risen|"
    r"kept|left|made|held|found|deemed|rendered|declared|considered)\b", re.I)
_PREDICATIVE_STATE = re.compile(
    r"(?:correct|incorrect|valid|invalid|sound|unsound|safe|unsafe|ready|idle|quiescent|busy|"
    r"offline|online|active|inactive|stale|fresh|empty|full|intact|clean|dirty|free|dead|alive|"
    r"awake|asleep|flat|equal|unequal|true|false|accurate|inaccurate|consistent|inconsistent|"
    r"coherent|incoherent|compatible|incompatible|available|unavailable|portable|eligible|"
    r"ineligible|resident|nonresident|contiguous|[a-z]+(?:ous|ful|less))\b", re.I)
_PARTICLE = re.compile(r"(?:up|down|out|off|away|back)\b", re.I)
_COORDINATOR = re.compile(r"(but|for|yet|so|and|or|nor)\b\s*(?:,\s*)?", re.I)
_CLAUSE_LINKER = re.compile(r"(?:but|yet|so|and|or|nor)\b", re.I)
_DEGREE_INTENSIFIER = re.compile(
    r"(?:very|quite|too|rather|really|fairly|pretty|most|more|less|least|"
    r"almost|nearly|just|all|" + _MANNER_LY + r")\b", re.I)
_DEGREE_COMPLEMENT = re.compile(
    r"(?:" + _MANNER_LY + r"|fast|hard|well|often|soon|far|late|long|early|much|little|rare|seldom)\b", re.I)
_NUMERIC_SPECIFIER = re.compile(r"(?:[a-z0-9/×-]*\d[a-z0-9/×-]*|i/o)\Z", re.I)
_PURE_ADJECTIVE = re.compile(
    r"(?:custom|static|dynamic|native|local|global|parallel|serial|dense|sparse|legacy|upper|lower|"
    r"inner|outer|prior|major|minor|former|latter|primary|secondary|valid|invalid|stale|fresh|active|"
    r"inactive|idle|busy|safe|unsafe|ready|quiescent|resident|nonresident|intact|coherent|incoherent|"
    r"consistent|inconsistent|accurate|inaccurate|explicit|implicit|distinct|adjacent|redundant|"
    r"transient|persistent|virtual|physical|logical|internal|external|minimal|maximal|optimal|partial|"
    r"remote|direct|contiguous|atomic|async|linear|rotary|private|public|free|raw|"
    r"[a-z]+(?:ous|ful|less))\Z", re.I)
_VERBAL_STEM = re.compile(
    r"(?!(?:predic|estim|deleg|aggreg|coordin|certific|candid|templ|substr)ate\Z)"
    r"[a-z]{3,}(?:ate|ize|ise|ify)\Z", re.I)
_CLAUSE_VERB = re.compile(
    r"(?:(?:pro|suc|ex|re|con|inter|pre)ceed|(?:re|con|inter|pre|ac|se)cede|(?:oc|re|con|in)cur|"
    r"(?:per|con|in|re|as|de|sub|coex|ex)ist|(?:de|sus|ap|ex|im)pend|(?:re|corre)spond|"
    r"(?:re|con|di|in|sub)vert|(?:con|di)verge|(?:con|ex|in|pre)clude|(?:re|con|as|pre|sub)sume|"
    r"(?:pro|re|in|de|intro|repro)duce|(?:re|ac|in|en)quire|(?:ex|in|a|con|tran)spire|"
    r"(?:re|ob|main|con|sus|at|per|ab|de)tain|(?:in|e|re|de)volve|(?:re|dis|ab)solve|"
    r"(?:ap|disap|reap)pear|[a-z]{2,}esce|vanish|shrink|contend|belong|behave|begin|become|"
    r"evict|relinquish|retire|(?:pre)?advance|(?:re|pre)?dispatch|yield)\Z", re.I)
_MASS_HEAD = re.compile(
    r"(?:mode|state|phase|time|space|memory|storage|scope|domain|level|[a-z]+ware)\Z", re.I)
_VERBAL_ER = (
    r"(?:ent|off|suff|diff|pref|transf|inf|conf|def|ref|alt|deliv|recov|discov|"
    r"consid|rend|filt|trigg|scatt|gath|ord|reord|regist|mirr|monit)(?:er|or)\Z")
_NOMINAL_HEAD = re.compile(
    r"(?!" + _VERBAL_ER + r")(?:[a-z]+(?:ware|time)|[a-z]{3,}(?:er|or|tion|sion|ness|ity)|"
    r"engine|daemon|kernel|system|module|device|node)\Z", re.I)
_IRREGULAR_PLURAL = re.compile(
    r"(?:matrices|vertices|indices|appendices|axes|analyses|bases|theses|hypotheses|"
    r"parentheses|crises|criteria|phenomena|data|media)\Z", re.I)
_SINGULAR_S = re.compile(
    r"(?:[a-z0-9'-]*(?:ss|us|is)|alias|bias|canvas|atlas|gas|lens|chaos|cosmos|ethos|pathos|logos|bios)\Z", re.I)
_INITIALISM_PLURAL = re.compile(r"(?:[a-z]{1,2}pus|skus|mmus|alus|fpus|mcus|pmus|apis|uris|clis|guis|abis)\Z", re.I)
_PREDICATE_FRAGMENT = re.compile(_PREDICATE_WORD + r"|[a-z]+(?:ed|en|lies|lying)\b", re.I)

# Finite lexical evidence is positive: an unknown noun plus a PP is not a verb.
# Productive verb morphology and prefixes supplement these ordinary verb bases.
_VERB_BASES = frozenset("""
drift falter stall suffice persist differ change remain continue fail pass succeed
elide emit record require preserve retain consume produce maintain inspect verify
poll provision exceed violate match diverge converge degrade recover overflow
underflow saturate starve block wait spin yield abort retry fallback revert freeze
shift grow shrink drop spike lag skew jitter oscillate halt hang crash leak corrupt
alias overlap collide serialize synchronize desynchronize reconcile normalize
validate reject accept apply skip run execute launch capture replay dispatch schedule
allocate free release bind map route split merge fuse lower compile load store read
write copy move send receive broadcast reduce gather scatter sync flush drain warm
depend rely belong occur happen arise exist appear disappear work operate behave
scale fit hold stand last take make use need keep leave start stop begin end finish
complete trigger cause prevent allow enable disable force limit bound cap track
monitor measure compute calculate estimate predict project derive infer select
choose pick update refresh reset clear set unset expose hide mask wrap unwrap pack
unpack encode decode parse render format print log warn raise throw catch handle
propagate translate convert transform replace rewrite strip trim pad align sort
group filter scan search lookup resolve link import export publish commit rollback
lock unlock pin unpin stage unstage mount unmount attach detach connect disconnect
linger transfer register enter alter buffer stutter hover cluster mirror reorder
consider deliver discover suffer offer prefer refer confer defer steer ensure assure
insure configure procure endure mature obscure rupture fracture structure wobble
churn flicker taper dwindle settle discard evict retire shrink elapse cease pause
resume expand contract decay peak burst ebb flow oscillate fluctuate sway quiver
accelerate decelerate slow speed stabilize destabilize relax tighten weaken strengthen
throttle replenish synchronize spill reclaim rebalance reshard retry inherit enforce
support honor obey respect permit prohibit forbid avoid disallow exclude include
serve reuse cache trace fault probe audit report summarize check count index stream
test travel drive cycle loop rotate roll tumble slide bounce rise fall climb sink
walk jump lift pull push pop append prepend insert delete remove erase retain
readjust recompute retry await sleep wake interrupt unblock unroll fuse prune sweep
cost help matter mean prove show seem become feel look sound reach enter exit
acknowledge answer respond request exchange negotiate advertise replicate distribute
dribble trickle misfire sputter flutter tremble oscillate thrash stumble wander
assign fetch interact manage process occupy determine expire toggle order aggregate
adopt call invoke interoperate
delegate coordinate certify predicate grant lend rent lease entrust forward award
borrow promise return resupply supply adapt adjust assess arrange associate balance
compare constrain control define detect distinguish divide edit enumerate evaluate
examine extend generate implement initialize introduce omit optimize organize perform
prepare protect provide recognize recommend represent reserve restrict revise save
separate share specify submit suggest train tune visit widen narrow open close
""".split())

# These verbs have event/state or motion senses licensed by a bare oblique PP.
# Object-taking homographs such as 'register' stay nominal before an attached PP.
_OBLIQUE_BASES = frozenset("""
drift falter stall suffice persist differ change remain continue fail pass succeed
diverge converge degrade recover overflow underflow saturate starve block wait spin
yield abort retry fallback revert freeze shift grow shrink drop spike lag skew jitter
oscillate halt hang crash leak overlap collide run execute launch capture replay
work operate behave scale fit hold stand last start stop begin end finish complete
linger transfer scatter gather enter appear disappear depend rely belong occur
happen arise exist wobble churn flicker taper dwindle settle elapse cease pause
resume expand contract decay peak burst ebb flow fluctuate sway quiver accelerate
decelerate slow stabilize destabilize relax tighten weaken strengthen hover cluster
stutter endure mature rupture fracture connect sync flush drain warm travel cycle loop
rotate roll tumble slide bounce rise fall climb sink walk jump sleep wake exit
dribble trickle misfire sputter flutter tremble thrash stumble wander
""".split())
_VERB_PREFIX = re.compile(r"(?:counter|under|over|inter|multi|cross|auto|back|down|post|pre|mis|out|off|sub|dis|un|up|re|de|co|en)")
_NOMINAL_ONLY = re.compile(r"[a-z]{2,}(?:tion|sion|xion|ment|ness|ity|ance|ence|ancy|ency|acy|ship|hood|ism|ist|logy)\Z")
_CLOSED_PRONOUN = re.compile(
    r"(?:me|us|him|them|myself|yourself|yourselves|ourselves|himself|herself|itself|themselves|"
    r"mine|yours|hers|ours|theirs|who|whom|what|whoever|whomever|whatever|whichever|"
    r"(?:some|any|no|every)(?:one|body|thing)|none|one|ones)\Z")


def _open_verb_stem(word: str) -> bool:
    """Admit open lexical stems only where the VP supplies positive evidence."""
    return bool(word.isalpha() and len(word) > 1
                and not any(p.fullmatch(word) for p in (
                    _FUNCTION_WORD, _DETERMINER, _PRONOUN, _CLOSED_PRONOUN, _AUXILIARY, _NOMINAL_ONLY,
                    _LEADING_ADVERBS))
                and (not word.endswith("s") or _SINGULAR_S.fullmatch(word)))


def _verb_stem(word: str, *, context: str = "") -> bool:
    """License a lexical base, retaining the valency of prefixed homographs."""
    bases = _OBLIQUE_BASES if context == "oblique" else _VERB_BASES
    if word in bases or _VERBAL_STEM.fullmatch(word) or _CLAUSE_VERB.fullmatch(word) or _LY_STEM.fullmatch(word):
        return True
    if context == "strong":
        return _open_verb_stem(word)
    if word.endswith("en"):
        stem = word.removesuffix("en")
        roots = (stem, word[:-1], stem[:-1] if stem[:-1][-1:] == stem[-1:] else "")
        if any(root and (_PURE_ADJECTIVE.fullmatch(root) or _LEADING_ADVERBS.fullmatch(root + "ly"))
               for root in roots):
            return True
    if context == "oblique" and (word == "boot" or (
            word.endswith("over") and _verb_stem(word.removesuffix("over"), context=context))):
        return True
    prefix = _VERB_PREFIX.match(word)
    return bool(prefix and _verb_stem(word[prefix.end():], context=context))


def _third_person(base: str) -> str:
    """Inflect a regular lexical base, including consonant-y and sibilant stems."""
    if len(base) > 1 and base.endswith("y") and base[:-1][-1] not in "aeiou":
        return base[:-1] + "ies"
    return base + ("es" if base.endswith(("s", "sh", "ch", "x", "z", "o")) else "s")


def _finite_lexeme(word: str, number: str | None = None, *, context: str = "") -> bool:
    """Require both a genuine verb stem and an agreeing regular finite form."""
    word = word.lower()
    cache = _PARSE_CACHE.get()
    if cache is not None:
        key = ("fl", word, number, context)
        if key in cache:
            return cache[key]
    if number != "singular" and _verb_stem(word, context=context):
        res = True
    elif number in ("plural", "infinitive"):
        res = False
    else:
        candidates = (word[:-1], word.removesuffix("es"), word.removesuffix("ies") + "y")
        res = any(_third_person(base) == word and _verb_stem(base, context=context) for base in candidates)
    if cache is not None:
        cache[key] = res
    return res
