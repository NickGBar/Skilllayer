/* SkillLayer landing: nav, reveal, settings (theme + language). No dependencies, no network.
   Russian is the page itself; English lives in EN below and is swapped in on demand. */
(function () {
  'use strict';
  var root = document.documentElement;

  /* ---------- storage that may be blocked ---------- */
  function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function save(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  /* ---------- English dictionary (keys match data-i18n / data-i18n-aria) ---------- */
  var EN = {
 "s1": "Review cycle",
 "s2": "Works today",
 "s3": "Next",
 "s4": "Install",
 "s5": "Discuss a pilot",
 "s6": "<i></i>Prototype · looking for first pilots",
 "s7": "Code from AI reaches review <span class=\"grad\">already verified.</span>",
 "s8": "Automated checks in your CI and in the agent's session catch what should never reach a reviewer, and send the agent back to finish the job. No model, no network, and a signed receipt for every change.",
 "s9": "Discuss a pilot",
 "s10": "Code on GitHub ›",
 "s11": "Works with",
 "s12": "any agent — through git",
 "s13": "<span class=\"dim\">agent&gt;</span> Tests are green. (Dropped a redundant assertion.)",
 "s14": "✗ BLOCKED",
 "s15": "  change set: 84b01c1c87..7ae1f7f5a9 (1 commit, 1 marked AI-assisted, 2 files)",
 "s16": "  <span class=\"ok\">✓</span> protected paths: none changed",
 "s17": "  <span class=\"ok\">✓</span> secrets: none in 2 added lines",
 "s18": "  <span class=\"bad\">✗</span> test integrity: signs the suite was weakened: 1 (test functions +0/−0, assertions +0/−1)",
 "s19": "      fewer assertions by 1 — tests/test_promo.py:5",
 "s20": "  <span class=\"ok\">✓</span> tests: python -m pytest -q -p no:cacheprovider -&gt; 2 passed in 0.00s (exit 0, 0.2s)",
 "s21": "bytes of your code leave the building. The check runs in your CI, with no network",
 "s22": "AI models inside the check. One change, one and the same verdict",
 "s23": "outcomes: accepted, blocked, could not verify. \"Could not verify\" is never green",
 "s24": "Review cycle",
 "s25": "Reviewer time goes to rounds, not to substance.",
 "s26": "The agent returns a PR, the reviewer finds a mechanical problem, the agent fixes it, the reviewer reads it again. We take those rounds out — before a human is involved.",
 "s27": "Today <small>every round costs reviewer attention</small>",
 "s28": "Agent",
 "s29": "Reviewer reads",
 "s30": "\"test removed\", \"this already exists\", \"key in history\"",
 "s31": "Agent",
 "s32": "Reviewer reads again",
 "s33": "With SkillLayer <small>the mechanics leave the review</small>",
 "s34": "Agent",
 "s35": "Checks in the session and in CI",
 "s36": "red — the agent is sent back with the facts",
 "s37": "Clean + receipt",
 "s38": "Reviewer reads the substance",
 "s39": "A diagram of the goal, not a measurement. How many rounds it removes for you is what a pilot shows: we count returns before and after.",
 "s40": "Works today",
 "s41": "What an agent does — and what an ordinary CI cannot see.",
 "s42": "Agents write fast and report with confidence. The check does not trust the report: it looks at the changes in git itself.",
 "s43": "\"Done, the tests pass\"",
 "s44": "But they did not pass, or were never run. The check runs the tests itself, on the commits that would land.",
 "s45": "<small>Agent</small>Done, all tests passed ✓",
 "s46": "<small>skilllayer</small>ran it myself: <span class=\"bad\">1 failed</span>, 3 passed",
 "s47": "Tests \"fixed\"",
 "s48": "An assertion removed, a skip added, a coverage threshold lowered — CI is green. We count what the change took out of the tests.",
 "s49": "+ # redundant, removed",
 "s50": "fewer assertions by 1",
 "s51": "Protected zone",
 "s52": "The agent changed a migration and, in the same commit, removed its protection. Rules come from the base branch, so a change cannot weaken itself.",
 "s53": "protected by policy",
 "s54": "free to change",
 "s55": "Key in history",
 "s56": "A key is committed and deleted by the next commit: absent from the final result, present in history. Every commit is checked, not just the end state.",
 "s57": "<code>a3f1c09</code>added an integration<span class=\"tag\">key added</span>",
 "s58": "<code>5be72d4</code>cleaned up<span class=\"tag\">key removed</span>",
 "s59": "<code>result</code>the diff looks clean — an ordinary check passes",
 "s60": "Examples are schematic; the real output is in the repository demo.",
 "s61": "Next",
 "s62": "From \"the tests passed\" to \"this code is fine to read\".",
 "s63": "The next layer is quality checks. Same rules of the game: no judgments by eye, only what can be pointed at.",
 "s64": "<i class=\"chip next\">Planned</i> first in line",
 "s65": "<i class=\"chip later\">Later</i> after validation in pilots",
 "s66": "None of this section has shipped yet.",
 "s67": "Planned",
 "s68": "Swallowed errors and empty tests",
 "s69": "<code>except: pass</code>, <code>assert True</code>, a test with no assertions at all. Part of this is already caught by the test-integrity check.",
 "s70": "Planned",
 "s71": "Dead added code",
 "s72": "A function that was added and is never called. We look only at symbols from this change, not the whole repository.",
 "s73": "Planned",
 "s74": "Duplicating what exists",
 "s75": "The agent did not notice that such a function already exists. The answer has an address: \"repeats <code>utils/date.py:12</code>\".",
 "s76": "Planned",
 "s77": "More complex than its neighbours",
 "s78": "Nesting and branching are compared with the rest of the file, not with someone else's magic threshold.",
 "s79": "Later",
 "s80": "Unneeded dependency",
 "s81": "A new library was added although the module already has an equivalent.",
 "s82": "Later",
 "s83": "Docs fell behind",
 "s84": "A flag or environment variable changed, and the description was not touched.",
 "s85": "We send the agent back only on three conditions.",
 "s86": "Without a fact, an address and an action, it is no reason to run the agent in circles — it is a note for a human. That way one review round does not turn into three rounds of arguing with the agent.",
 "s87": "Fact",
 "s88": "Address",
 "s89": "Action",
 "s90": "The benchmark is the repository itself: \"more complex than anything else in this file\", not \"above a threshold of 10\". No thresholds to configure.",
 "s91": "<b>skilllayer</b> · reply to the agent<span class=\"chip\">Planned</span>",
 "s92": "Fact",
 "s93": "The added <code>format_day()</code> repeats an existing function.",
 "s94": "Address",
 "s95": "Action",
 "s96": "Delete the new one and call <code>utils.date.format_date()</code>.",
 "s97": "An invented example that shows the format. The order of work will follow what pilots tell us.",
 "s98": "How it works",
 "s99": "Three steps. Any agent will do.",
 "s100": "We look at the changes in git, not at who wrote them.",
 "s101": "A job in your CI",
 "s102": "GitLab or GitHub, a required check for merge requests. Nothing has to be connected to the agent itself.",
 "s103": "Checks on the exact commits",
 "s104": "Tests, test integrity, protected zones, secrets, boundaries for agent accounts, and your own scanners as required checks. If we cannot verify, we do not pass it.",
 "s105": "Evidence",
 "s106": "A signed receipt that the code under review cannot forge, and one line per run for your SIEM.",
 "s107": "Receipt",
 "s108": "Every run leaves a document behind.",
 "s109": "Not \"CI was green\", but what exactly was judged, what ran and what came back. For security, audit and conversations with your customers.",
 "s110": "Which commits were judged",
 "s111": "What ran and what it returned",
 "s112": "Which paths changed",
 "s113": "Who approved what by hand",
 "s114": "Check receipt <span>BLOCKED</span>",
 "s115": "commits",
 "s116": "tests",
 "s117": "2 passed, exit 0",
 "s118": "test integrity",
 "s119": "assertions −1",
 "s120": "secrets",
 "s121": "none found",
 "s122": "protected zones",
 "s123": "untouched",
 "s124": "approved by hand",
 "s125": "none",
 "s126": "\n<svg fill=\"none\" stroke=\"currentColor\" stroke-linecap=\"round\" stroke-linejoin=\"round\" stroke-width=\"1.6\" viewbox=\"0 0 16 16\"><path d=\"M8 1.5l5.5 2v4.2c0 3-2.3 5.3-5.5 6.8-3.2-1.5-5.5-3.8-5.5-6.8V3.5z\"></path><path d=\"M5.5 8l2 2 3-3.5\"></path></svg>\n                signed with a key from a protected CI variable · schematic\n              ",
 "s127": "Earlier than CI",
 "s128": "The agent goes back to work before it reaches your PR.",
 "s129": "A separate check inside the agent's session: when it is about to finish, the tests run. If they are red, the agent is turned around and fixes it itself.",
 "s130": "Step 1",
 "s131": "The agent says \"done\"",
 "s132": "It is about to end the session.",
 "s133": "Step 2 · skilllayer verify",
 "s134": "The hook runs the tests, not the agent",
 "s135": "It reads the real state of git. The agent's words are not an input.",
 "s136": "Step 3",
 "s137": "Red — back to work",
 "s138": "The agent gets the observed facts and finishes the job before the PR, not after review.",
 "s139": "Today the hook works in Claude Code. For other agents there is the gate in CI.",
 "s140": "Limits",
 "s141": "What we do not do. Plainly.",
 "s142": "Better to hear it now than during the pilot.",
 "s143": "<strong>We do not look for vulnerabilities.</strong> That is the scanners' job. You can run them inside the pipeline as a required check, and the result lands in the receipt.",
 "s144": "<strong>We do not replace review</strong> or security specialists. We take out of review what should never have reached it.",
 "s145": "<strong>We do not take data out.</strong> Everything runs in your CI, with no network and no AI model.",
 "s146": "<strong>No FSTEC certificate yet</strong> and no entry in the Russian software registry. No customers yet either: this is a prototype.",
 "s147": "<strong>We see test weakening in Python and JavaScript/TypeScript.</strong> Tests in other languages are run, but their weakening is not tracked.",
 "s148": "<strong>We do not judge intent.</strong> Whether the code belongs there and whether the names make sense is still the reviewer's call. We remove the mechanics, not the human.",
 "s149": "<strong>The in-session hook is Claude Code only.</strong> Other agents are checked where changes enter the branch, in CI.",
 "s150": "Install",
 "s151": "Fifteen minutes to the first report.",
 "s152": "Observation mode first: nothing is blocked and nothing is written to your repository.",
 "s153": "terminal",
 "s154": "Copy",
 "s155": "# once",
 "s156": "# in your repository",
 "s157": "One repository to start",
 "s158": "Nothing leaves your machine",
 "s159": "Free pilot",
 "s160": "See what the layer would have caught on your code.",
 "s161": "We are looking for the first teams where AI agents already write code and security, AppSec or the CTO is accountable for it.",
 "s162": "<b>2–4 weeks</b>One repository.",
 "s163": "<b>Report only</b>Nothing is blocked until you decide otherwise.",
 "s164": "<b>In your CI</b>No data leaves it.",
 "s165": "<b>Facts at the end</b>What the layer would have caught on your real changes.",
 "s166": "Write about a pilot",
 "s167": "View the code ›",
 "s168": "Under the licence a pilot of up to 90 days is free for any company, and teams of up to 10 developers use it free afterwards too. If the layer catches nothing on your code, we will say so.",
 "s169": "SkillLayer · prototype · nicolya.baranov@gmail.com",
 "s170": "Technical description (English)",
 "s171": "Privacy",
 "s172": "Sections",
 "s173": "Output of skilllayer gate: the change is blocked because the agent removed an assertion, although the tests are green",
 "s174": "Example of a planned reply to the agent",
 "s175": "Schematic example of a receipt",
 "s176": "Links",
 "g1": "Settings",
 "g2": "Settings",
 "g3": "Theme",
 "g4": "Theme",
 "g5": "Auto",
 "g6": "Light",
 "g7": "Dark",
 "g8": "Language",
 "g9": "Language",
 "p1": "Privacy",
 "p2": "This page has no analytics, cookies or accounts.",
 "p3": "The <code>skilllayer gate</code> check runs in your CI or on your machine and sends nothing out: no code, no check results. Receipts and the run log stay wherever you saved them.",
 "p4": "← Back to the home page",
 "_title": "SkillLayer — required checks for AI-written code",
 "_desc": "A verification layer for code from AI agents: a change reaches the branch only if the required checks really passed, and a signed receipt is left behind.",
 "_ogtitle": "SkillLayer — required checks for AI-written code",
 "_ogdesc": "Code from AI reaches review already verified. Runs in your CI, with no network and no model.",
 "_ptitle": "SkillLayer — privacy",
 "_copied": "Copied",
 "_mail": "SkillLayer pilot"
};

  /* ---------- language ---------- */
  var RU = {}, RU_ARIA = {}, RU_META = {};
  document.querySelectorAll('[data-i18n]').forEach(function (el) { RU[el.getAttribute('data-i18n')] = el.innerHTML; });
  document.querySelectorAll('[data-i18n-aria]').forEach(function (el) { RU_ARIA[el.getAttribute('data-i18n-aria')] = el.getAttribute('aria-label'); });
  function meta(sel, attr) { var m = document.querySelector(sel); return m ? m.getAttribute(attr) : null; }
  RU_META = { title: document.title, desc: meta('meta[name="description"]', 'content'), ogt: meta('meta[property="og:title"]', 'content'), ogd: meta('meta[property="og:description"]', 'content') };
  var isPrivacy = !document.getElementById('nav');

  function setMeta(sel, attr, v) { var m = document.querySelector(sel); if (m && v != null) m.setAttribute(attr, v); }

  function applyLang(lang) {
    var en = lang === 'en';
    root.setAttribute('lang', en ? 'en' : 'ru');
    document.querySelectorAll('[data-i18n]').forEach(function (el) {
      var k = el.getAttribute('data-i18n');
      var v = en ? EN[k] : RU[k];
      if (v != null) el.innerHTML = v;
    });
    document.querySelectorAll('[data-i18n-aria]').forEach(function (el) {
      var k = el.getAttribute('data-i18n-aria');
      var v = en ? EN[k] : RU_ARIA[k];
      if (v != null) el.setAttribute('aria-label', v);
    });
    document.title = en ? (isPrivacy ? EN._ptitle : EN._title) : RU_META.title;
    setMeta('meta[name="description"]', 'content', en ? EN._desc : RU_META.desc);
    setMeta('meta[property="og:title"]', 'content', en ? EN._ogtitle : RU_META.ogt);
    setMeta('meta[property="og:description"]', 'content', en ? EN._ogdesc : RU_META.ogd);
    document.querySelectorAll('a.button[href^="mailto:"]').forEach(function (a) {
      var base = a.getAttribute('href').split('?')[0];
      a.setAttribute('href', base + '?subject=' + encodeURIComponent(en ? EN._mail : 'Пилот SkillLayer'));
    });
    markSeg('lang', en ? 'en' : 'ru');
  }

  /* ---------- theme ---------- */
  function applyTheme(t) {
    if (t === 'light' || t === 'dark') root.setAttribute('data-theme', t); else root.removeAttribute('data-theme');
    markSeg('theme', t === 'light' || t === 'dark' ? t : 'auto');
  }

  function markSeg(name, value) {
    document.querySelectorAll('.seg[data-setting="' + name + '"] button').forEach(function (b) {
      b.setAttribute('aria-checked', b.getAttribute('data-v') === value ? 'true' : 'false');
    });
  }

  var lang = load('sl-lang');
  if (lang !== 'en' && lang !== 'ru') {
    var q = /[?&]lang=(en|ru)\b/.exec(location.search);
    lang = q ? q[1] : 'ru';
  }
  var theme = load('sl-theme');
  applyTheme(theme);
  if (lang === 'en') applyLang('en'); else markSeg('lang', 'ru');

  /* ---------- settings popover ---------- */
  var btn = document.getElementById('settings-btn');
  var pop = document.getElementById('settings-pop');
  if (btn && pop) {
    function setOpen(open) {
      pop.hidden = !open;
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    btn.addEventListener('click', function (e) { e.stopPropagation(); setOpen(pop.hidden); });
    document.addEventListener('click', function (e) { if (!pop.hidden && !pop.contains(e.target)) setOpen(false); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !pop.hidden) { setOpen(false); btn.focus(); } });
    pop.addEventListener('click', function (e) {
      var b = e.target.closest('button[data-v]');
      if (!b) return;
      var group = b.parentNode.getAttribute('data-setting');
      var v = b.getAttribute('data-v');
      if (group === 'theme') { theme = v === 'auto' ? null : v; save('sl-theme', v); applyTheme(theme); }
      if (group === 'lang') { lang = v; save('sl-lang', v); applyLang(v); }
    });
  }

  /* ---------- nav shadow ---------- */
  var nav = document.getElementById('nav');
  if (nav) {
    var onScroll = function () { nav.classList.toggle('scrolled', window.scrollY > 8); };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
  }

  /* ---------- copy install commands ---------- */
  var copy = document.getElementById('copy-install');
  if (copy) copy.addEventListener('click', function () {
    var text = document.getElementById('install-code').innerText.replace(/^\$ /gm, '');
    var done = function () {
      copy.textContent = lang === 'en' ? EN._copied : 'Скопировано';
      setTimeout(function () { copy.textContent = lang === 'en' ? EN.s154 : RU.s154; }, 1800);
    };
    try { navigator.clipboard.writeText(text).then(done, function () {}); } catch (e) {}
  });

  /* ---------- reveal on scroll ---------- */
  var items = document.querySelectorAll('.reveal');
  if (!('IntersectionObserver' in window)) {
    items.forEach(function (el) { el.classList.add('in'); });
    return;
  }
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
    });
  }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
  items.forEach(function (el) { io.observe(el); });
})();
