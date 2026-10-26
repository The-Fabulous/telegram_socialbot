const fs = require('fs');
const path = require('path');

const W = 2400;
const H = 2400;
const TARGET = 400;
const GAP = 4;
let seed = 0x534f4349;
const rand = () => {
  seed = Math.imul(seed ^ seed >>> 15, 1 | seed) + 0x6D2B79F5 | 0;
  return ((seed ^ seed >>> 14) >>> 0) / 4294967296;
};
const int = (min, max) => min + Math.floor(rand() * (max - min + 1));
const pick = items => items[Math.floor(rand() * items.length)];
const esc = text => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const COLORS = ['#26392C', '#1D3023', '#15251A'];
const VARS = ['x', 'y', 'z', 't', 'u', 'v', 'r', 'n', 'k', 'θ', 'α', 'β', 'λ', 'μ'];

const FORMULAS = [
  ['quadratic', p => `${p.a}${p.x}² + ${p.b}${p.x} − ${p.c} = 0`],
  ['square-identity', p => `(${p.x}+${p.a})² = ${p.x}² + ${2 * p.a}${p.x} + ${p.a * p.a}`],
  ['difference-squares', p => `(${p.x}−${p.a})(${p.x}+${p.a}) = ${p.x}²−${p.a * p.a}`],
  ['factor-polynomial', p => `${p.x}² + ${p.a + p.b}${p.x} + ${p.a * p.b} = (${p.x}+${p.a})(${p.x}+${p.b})`],
  ['quadratic-roots', p => `${p.x} = (−${p.b} ± √(${p.b * p.b}−4ac)) / 2a`],
  ['cubic', p => `${p.x}³ − ${p.a}${p.x}² + ${p.b}${p.x} − ${p.c} = 0`],
  ['binomial', p => `(${p.x}+${p.y})³ = ${p.x}³ + 3${p.x}²${p.y} + 3${p.x}${p.y}² + ${p.y}³`],
  ['rational-identity', p => `1/${p.x} + 1/${p.y} = (${p.x}+${p.y})/(${p.x}${p.y})`],
  ['inequality', p => `${p.a}/${p.b} < ${p.x}/${p.y} ≤ ${p.c}/${p.a + 1}`],
  ['radical', p => `√(${p.a}${p.x}+${p.b}) = ${p.y} − ${p.c}`],
  ['log-product', p => `log_${p.a}(${p.x}${p.y}) = log_${p.a}${p.x} + log_${p.a}${p.y}`],
  ['log-power', p => `log_${p.a}(${p.x}^${p.b}) = ${p.b} log_${p.a}${p.x}`],
  ['exponential', p => `${p.a}^${p.x} · ${p.a}^${p.y} = ${p.a}^(${p.x}+${p.y})`],
  ['binomial-factor', p => `${p.x}⁴ − ${p.a}⁴ = (${p.x}−${p.a})(${p.x}+${p.a})(${p.x}²+${p.a}²)`],
  ['absolute-value', p => `|${p.x}−${p.a}| + |${p.x}+${p.a}| ≥ 2${p.a}`],
  ['complex-roots', p => `z² + ${p.a}z + ${p.b} = 0,  z₁z₂ = ${p.b}`],
  ['derivative-power', p => `d/d${p.x} [${p.x}^${p.n}] = ${p.n}${p.x}^${p.n - 1}`],
  ['derivative-product', p => `(fg)′ = f′g + fg′`],
  ['derivative-chain', p => `d/d${p.x} sin(${p.a}${p.x}) = ${p.a} cos(${p.a}${p.x})`],
  ['partial-derivative', p => `∂f/∂${p.x} = ${p.a}${p.x}²${p.y} + ${p.b}${p.y}`],
  ['gradient', p => `∇f = (∂f/∂${p.x}, ∂f/∂${p.y})`],
  ['limit', p => `lim ${p.x}→${p.a} (${p.x}²−${p.a * p.a})/(${p.x}−${p.a}) = ${2 * p.a}`],
  ['integral-power', p => `∫ ${p.x}^${p.n} d${p.x} = ${p.x}^${p.n + 1}/${p.n + 1} + C`],
  ['definite-integral', p => `∫₀^π sin(${p.n}${p.x}) d${p.x} = ${p.n % 2 ? `2/${p.n}` : '0'}`],
  ['integral-substitution', p => `∫ 2${p.x} cos(${p.x}²) d${p.x} = sin(${p.x}²) + C`],
  ['contour-integral', p => `∮_C F·dr = ∬_D (∇×F)·dA`],
  ['series', p => `Σₖ₌₁^∞ 1/k^${p.n} = ζ(${p.n})`],
  ['finite-sum', p => `Σₖ₌₁^${p.n} k = ${p.n}(${p.n + 1})/2`],
  ['taylor', p => `e^${p.x} = 1 + ${p.x} + ${p.x}²/2! + ${p.x}³/3! + …`],
  ['differential-equation', p => `dy/d${p.x} + ${p.a}y = e^(−${p.x})`],
  ['wave-equation', p => `∂²u/∂t² = ${p.a}² ∂²u/∂${p.x}²`],
  ['heat-equation', p => `∂u/∂t = ${p.a} ∂²u/∂${p.x}²`],
  ['trig-pythagorean', p => `sin²${p.x} + cos²${p.x} = 1`],
  ['trig-double-angle', p => `sin(2${p.x}) = 2 sin${p.x} cos${p.x}`],
  ['trig-cosine', p => `cos(2${p.x}) = cos²${p.x} − sin²${p.x}`],
  ['trig-addition', p => `tan(${p.x}+${p.y}) = (tan${p.x}+tan${p.y})/(1−tan${p.x}tan${p.y})`],
  ['sine-rule', p => `a/sin α = b/sin β = c/sin γ`],
  ['cosine-rule', p => `c² = a² + b² − 2ab cos γ`],
  ['unit-circle', p => `(${p.x}, ${p.y}) = (cos θ, sin θ)`],
  ['euler-identity', p => `e^(iπ) + 1 = 0`],
  ['complex-modulus', p => `z = ${p.a} + ${p.b}i,  |z| = √${p.a * p.a + p.b * p.b}`],
  ['complex-polar', p => `z = r(cos θ + i sin θ) = re^(iθ)`],
  ['matrix-product', p => `[${p.a} ${p.b}; ${p.c} ${p.n}] [x; y] = [u; v]`],
  ['determinant', p => `det(A−λI) = 0,  λ = ${p.a} or ${p.b}`],
  ['eigenvector', p => `Av = λv,  det(A−λI) = 0`],
  ['vector-dot', p => `a·b = |a||b| cos θ = ${p.a * p.b}`],
  ['cross-product', p => `|a×b| = |a||b| sin θ`],
  ['linear-system', p => `${p.a}x + ${p.b}y = ${p.c};  ${p.b}x − ${p.a}y = ${p.n}`],
  ['probability', p => `P(A∩B) = P(A)P(B|A)`],
  ['expectation', p => `E[X] = Σᵢ xᵢ pᵢ,  Σᵢ pᵢ = 1`],
  ['normalization', p => `∫₋∞^∞ ρ(x) dx = 1`],
  ['set-distributive', p => `A∪(B∩C) = (A∪B)∩(A∪C)`],
  ['set-inclusion', p => `A⊆B ⇒ A∩C ⊆ B∩C`],
  ['cardinality', p => `|A∪B| = |A| + |B| − |A∩B|`],
  ['golden-ratio', p => `φ = (1+√5)/2,  φ² = φ + 1`],
  ['pythagorean', p => `a² + b² = c²,  c = √(${p.a * p.a}+${p.b * p.b})`],
  ['circle-area', p => `A = πr²,  C = 2πr`],
  ['cone-volume', p => `V = ⅓πr²h,  A = πr(r+ℓ)`],
  ['sphere-volume', p => `V = ⁴⁄₃πr³,  A = 4πr²`],
  ['distance', p => `d = √[(${p.x}₂−${p.x}₁)² + (${p.y}₂−${p.y}₁)²]`],
  ['slope', p => `m = (y₂−y₁)/(x₂−x₁),  y = mx + b`],
  ['area-triangle', p => `S = ½ab sin θ = ${p.a * p.b}/2`],
  ['mean-value', p => `f′(c) = [f(b)−f(a)]/(b−a)`],
  ['epsilon-delta', p => `∀ ε>0  ∃ δ>0 : |${p.x}−${p.a}|<δ`],
  ['gamma-function', p => `Γ(n+1) = ∫₀^∞ t^n e^(−t) dt = n!`],
  ['fourier', p => `f(x) = a₀ + Σₙ (aₙ cos nx + bₙ sin nx)`],
  ['divergence', p => `∇·F = ∂Fₓ/∂x + ∂Fᵧ/∂y + ∂F_z/∂z`],
];

function makeFormula() {
  const family = pick(FORMULAS);
  const a = int(2, 13), b = int(2, 17), c = int(1, 19), n = int(2, 9);
  const x = pick(VARS), y = pick(VARS.filter(v => v !== x));
  return {family: family[0], text: family[1]({a, b, c, n, x, y})};
}

function diagram(index, w, h) {
  const s = 'fill="none" vector-effect="non-scaling-stroke"';
  const label = (x, y, value) => `<text x="${x}" y="${y}" font-size="12">${value}</text>`;
  const axis = `M10 ${h - 12}H${w - 8}M20 ${h - 6}V8`;
  const types = [
    () => `<path ${s} d="M10 ${h - 10}L${w * .5} 10L${w - 10} ${h - 10}ZM${w * .5} 10V${h - 10}"/>${label(9, h - 13, 'A')}${label(w - 20, h - 13, 'B')}${label(w * .52, 20, 'C')}`,
    () => `<circle ${s} cx="${w * .49}" cy="${h * .51}" r="${Math.min(w, h) * .4}"/><path ${s} d="M${w * .49} ${h * .51}L${w * .79} ${h * .3}L${w * .79} ${h * .51}Z"/>${label(w * .64, h * .43, 'θ')}`,
    () => `<path ${s} d="M18 28L${w * .48} 12L${w - 16} 30V${h - 24}L${w * .48} ${h - 12}L18 ${h - 25}ZM18 28V${h - 25}M${w * .48} 12V${h - 12}M${w - 16} 30L${w * .48} ${h - 12}M18 28L${w * .48} ${h * .52}L${w - 16} 30M${w * .48} ${h * .52}V${h - 12}"/><path ${s} d="M18 ${h - 25}L${w * .48} ${h * .52}" stroke-dasharray="4 4"/>`,
    () => `<ellipse ${s} cx="${w * .5}" cy="20" rx="${w * .35}" ry="12"/><path ${s} d="M${w * .15} 20V${h - 20}M${w * .85} 20V${h - 20}"/><ellipse ${s} cx="${w * .5}" cy="${h - 20}" rx="${w * .35}" ry="12"/><path ${s} d="M${w * .15} ${h - 20}Q${w * .5} ${h - 4} ${w * .85} ${h - 20}" stroke-dasharray="4 4"/>`,
    () => `<ellipse ${s} cx="${w * .5}" cy="${h * .5}" rx="${w * .4}" ry="${h * .4}"/><ellipse ${s} cx="${w * .5}" cy="${h * .5}" rx="${w * .4}" ry="${h * .13}"/><path ${s} d="M${w * .5} ${h * .1}C${w * .22} ${h * .3} ${w * .22} ${h * .7} ${w * .5} ${h * .9}M${w * .5} ${h * .1}C${w * .78} ${h * .3} ${w * .78} ${h * .7} ${w * .5} ${h * .9}"/>`,
    () => `<path ${s} d="${axis}M24 ${h * .76}Q${w * .48} ${h * .06} ${w - 18} ${h * .74}"/>${label(w - 22, h - 19, 'x')}${label(8, 17, 'y')}`,
    () => `<path ${s} d="${axis}M22 ${h * .5}C${w * .26} ${h * .1} ${w * .4} ${h * .9} ${w * .56} ${h * .5}S${w * .84} ${h * .1} ${w - 12} ${h * .5}"/>${label(w * .68, 18, 'sin x')}`,
    () => `<path ${s} d="${axis}M${w * .26} ${h * .22}Q${w * .5} ${h * .92} ${w * .74} ${h * .22}M${w * .26} ${h * .78}Q${w * .5} ${h * .08} ${w * .74} ${h * .78}"/>`,
    () => `<path ${s} d="M${w * .5} 10L${w - 12} ${h - 12}H12ZM${w * .5} 10L${w * .5} ${h - 12}M${w * .5} 10L${w * .68} ${h * .63}M12 ${h - 12}Q${w * .5} ${h * .78} ${w - 12} ${h - 12}" stroke-dasharray="5 4"/>${label(w * .53, h * .54, 'h')}`,
    () => `<path ${s} d="M${w * .5} 10L${w * .88} ${h * .84}H${w * .12}ZM${w * .5} 10V${h * .84}M${w * .5} 10L${w * .28} ${h * .84}M${w * .5} 10L${w * .72} ${h * .84}"/>${label(w * .52, h * .58, 'h')}`,
    () => `<path ${s} d="M16 30H${w * .58}V${h - 18}H16ZM${w * .3} 12H${w - 12}V${h * .55}H${w * .3}ZM16 30L${w * .3} 12M${w * .58} 30L${w - 12} 12M${w * .58} ${h - 18}L${w - 12} ${h * .55}"/><path ${s} d="M16 ${h - 18}L${w * .3} ${h * .55}" stroke-dasharray="4 4"/>`,
    () => `<path ${s} d="M15 ${h - 16}H${w - 10}M22 ${h - 8}V10M26 ${h * .73}L${w * .72} ${h * .25}M${w * .72} ${h * .25}L${w * .65} ${h * .43}M${w * .72} ${h * .25}L${w * .52} ${h * .29}"/>${label(w * .74, h * .2, 'v')}`,
    () => `<circle ${s} cx="${w * .34}" cy="${h * .57}" r="${Math.min(w, h) * .3}"/><path ${s} d="M${w * .34} ${h * .57}L${w * .63} ${h * .31}M${w * .62} ${h * .31}L${w * .93} ${h * .31}"/>${label(w * .59, h * .25, 't')}`,
    () => `<ellipse ${s} cx="${w * .5}" cy="${h * .5}" rx="${w * .42}" ry="${h * .3}"/><circle cx="${w * .3}" cy="${h * .5}" r="2.5"/><circle cx="${w * .7}" cy="${h * .5}" r="2.5"/><path ${s} d="M${w * .3} ${h * .5}H${w * .7}M${w * .5} ${h * .2}V${h * .8}" stroke-dasharray="4 4"/>${label(w * .28, h * .43, 'F₁')}${label(w * .68, h * .43, 'F₂')}`,
    () => `<path ${s} d="${axis}M24 ${h * .76}Q${w * .5} ${h * .08} ${w - 16} ${h * .78}"/><path ${s} d="M${w * .55} ${h * .48}L${w * .76} ${h * .48}M${w * .55} ${h * .48}L${w * .65} ${h * .23}" stroke-dasharray="4 4"/><circle cx="${w * .65}" cy="${h * .36}" r="2.5"/>`,
    () => `<rect ${s} x="${w * .12}" y="${h * .14}" width="${w * .28}" height="${h * .3}"/><rect ${s} x="${w * .58}" y="${h * .14}" width="${w * .28}" height="${h * .3}"/><rect ${s} x="${w * .12}" y="${h * .59}" width="${w * .28}" height="${h * .3}"/><rect ${s} x="${w * .58}" y="${h * .59}" width="${w * .28}" height="${h * .3}"/>${label(w * .46, h * .35, '×')}${label(w * .46, h * .8, '+')}`,
    () => `<path ${s} d="${axis}M24 ${h * .72}Q${w * .42} ${h * .16} ${w - 16} ${h * .28}L${w - 16} ${h - 12}H24Z"/><path ${s} d="M${w * .45} ${h * .5}V${h - 12}" stroke-dasharray="4 4"/>${label(w * .48, h * .82, '∫')}`,
    () => `<path ${s} d="M${w * .5} 12L${w * .5} ${h - 12}M12 ${h * .5}H${w - 12}M${w * .18} ${h * .78}L${w * .78} ${h * .2}"/><path ${s} d="M${w * .63} ${h * .2}H${w * .78}V${h * .35}"/>${label(w * .8, h * .19, 'v')}`,
    () => `<path ${s} d="M14 ${h - 12}H${w - 10}M22 ${h - 6}V10M26 ${h * .72}Q${w * .44} ${h * .08} ${w - 18} ${h * .28}"/><path ${s} d="M${w * .49} ${h * .48}L${w * .49} ${h - 12}" stroke-dasharray="4 4"/>${label(w * .51, h * .44, 'c')}`,
    () => `<circle ${s} cx="${w * .5}" cy="${h * .5}" r="${Math.min(w, h) * .38}"/><path ${s} d="M${w * .5} ${h * .5}V${h * .12}M${w * .5} ${h * .5}L${w * .82} ${h * .7}"/>${label(w * .53, h * .23, 'θ')}${label(w * .43, h * .56, 'O')}`,
    () => `<path ${s} d="M${w * .5} ${h * .12}L${w * .9} ${h * .82}H${w * .1}ZM${w * .5} ${h * .12}L${w * .5} ${h * .82}M${w * .1} ${h * .82}L${w * .69} ${h * .47}" stroke-dasharray="5 4"/>${label(w * .7, h * .45, 'p')}`,
    () => `<path ${s} d="${axis}M24 ${h * .72}Q${w * .5} ${h * .1} ${w - 18} ${h * .72}"/><path ${s} d="M${w * .3} ${h * .38}L${w * .73} ${h * .38}" stroke-dasharray="3 4"/>${label(w * .75, h * .35, 'y=c')}`,
    () => `<path ${s} d="M${w * .5} ${h * .5}L${w * .88} ${h * .5}M${w * .5} ${h * .5}L${w * .68} ${h * .19}M${w * .5} ${h * .5}L${w * .25} ${h * .78}"/><path ${s} d="M${w * .7} ${h * .5}L${w * .82} ${h * .5}M${w * .5} ${h * .28}L${w * .56} ${h * .18}"/>${label(w * .89, h * .48, 'x')}${label(w * .69, h * .16, 'y')}${label(w * .18, h * .85, 'z')}`,
    () => `<circle ${s} cx="${w * .5}" cy="${h * .5}" r="${Math.min(w, h) * .34}"/><path ${s} d="M${w * .5} ${h * .5}L${w * .84} ${h * .5}M${w * .5} ${h * .5}L${w * .5} ${h * .16}M${w * .5} ${h * .5}L${w * .28} ${h * .72}"/>${label(w * .66, h * .43, 'r')}`,
    () => `<path ${s} d="${axis}M24 ${h * .5}C${w * .36} ${h * .05} ${w * .64} ${h * .95} ${w - 18} ${h * .5}M24 ${h * .5}C${w * .36} ${h * .95} ${w * .64} ${h * .05} ${w - 18} ${h * .5}"/>${label(w * .78, h * .37, '±')}`,
    () => `<path ${s} d="M16 ${h - 15}H${w - 10}M22 ${h - 8}V10M25 ${h * .72}Q${w * .32} ${h * .08} ${w * .49} ${h * .53}T${w - 16} ${h * .25}"/>${label(w * .73, h * .2, 'f(x)')}`,
    () => `<path ${s} d="M${w * .5} ${h * .12}L${w * .87} ${h * .8}H${w * .13}ZM${w * .5} ${h * .12}L${w * .32} ${h * .8}M${w * .5} ${h * .12}L${w * .69} ${h * .8}M${w * .32} ${h * .8}L${w * .5} ${h * .49}L${w * .69} ${h * .8}" stroke-dasharray="4 4"/>${label(w * .51, h * .47, 'm')}`,
    () => `<path ${s} d="M${w * .12} ${h * .82}L${w * .5} ${h * .14}L${w * .88} ${h * .82}ZM${w * .12} ${h * .82}L${w * .72} ${h * .55}L${w * .5} ${h * .14}M${w * .72} ${h * .55}V${h * .82}" stroke-dasharray="4 4"/>${label(w * .74, h * .53, 'h')}`,
    () => `<path ${s} d="${axis}M${w * .18} ${h * .78}L${w * .81} ${h * .2}M${w * .2} ${h * .18}L${w * .82} ${h * .8}"/><circle cx="${w * .5}" cy="${h * .5}" r="3"/>${label(w * .51, h * .47, 'z')}`,
    () => `<ellipse ${s} cx="${w * .5}" cy="${h * .53}" rx="${w * .4}" ry="${h * .26}"/><path ${s} d="M${w * .1} ${h * .53}L${w * .5} ${h * .15}L${w * .9} ${h * .53}M${w * .5} ${h * .15}V${h * .88}" stroke-dasharray="4 4"/>${label(w * .51, h * .14, 'P')}`,
    () => `<path ${s} d="M${w * .13} ${h * .82}L${w * .85} ${h * .22}M${w * .13} ${h * .22}L${w * .85} ${h * .82}M${w * .5} ${h * .1}V${h * .9}M${w * .1} ${h * .5}H${w * .9}"/><circle cx="${w * .5}" cy="${h * .5}" r="2.5"/>${label(w * .52, h * .47, 'i')}`,
    () => `<path ${s} d="${axis}M24 ${h * .78}Q${w * .33} ${h * .08} ${w * .48} ${h * .57}T${w - 18} ${h * .24}M${w * .48} ${h * .57}V${h - 12}" stroke-dasharray="4 4"/>${label(w * .5, h * .82, 'a')}`,
    () => `<path ${s} d="M${w * .15} ${h * .82}L${w * .84} ${h * .19}M${w * .2} ${h * .22}L${w * .8} ${h * .79}M${w * .35} ${h * .36}L${w * .65} ${h * .64}" stroke-dasharray="4 4"/><circle cx="${w * .5}" cy="${h * .5}" r="3"/>${label(w * .53, h * .48, 'P')}`,
  ];
  return types[index % types.length]();
}

const placed = [];
const CELL = 128;
const REPEAT_RADIUS = 260;
const grid = new Map();
let rejectedCollisionCandidates = 0;
let rejectedSimilarityCandidates = 0;
const key = (x, y) => `${x},${y}`;

function cellsFor(o, dx = 0, dy = 0, margin = GAP) {
  const x0 = Math.floor((o.x + dx - o.bw / 2 - margin) / CELL);
  const x1 = Math.floor((o.x + dx + o.bw / 2 + margin) / CELL);
  const y0 = Math.floor((o.y + dy - o.bh / 2 - margin) / CELL);
  const y1 = Math.floor((o.y + dy + o.bh / 2 + margin) / CELL);
  const cells = [];
  for (let x = x0; x <= x1; x++) for (let y = y0; y <= y1; y++) cells.push(key(x, y));
  return cells;
}

function nearby(o) {
  const found = new Set();
  for (const dx of [-W, 0, W]) for (const dy of [-H, 0, H]) {
    for (const cell of cellsFor(o, dx, dy, REPEAT_RADIUS)) for (const item of grid.get(cell) || []) found.add(item);
  }
  return found;
}

function wrappedDistance(a, b, period) {
  const distance = Math.abs(a - b);
  return Math.min(distance, period - distance);
}

function collides(o) {
  for (const other of nearby(o)) {
    const dx = wrappedDistance(o.x, other.x, W);
    const dy = wrappedDistance(o.y, other.y, H);
    if (o.family === other.family && Math.hypot(dx, dy) < REPEAT_RADIUS) return 'similarity';
    if (dx < (o.bw + other.bw) / 2 + GAP && dy < (o.bh + other.bh) / 2 + GAP) return 'collision';
  }
  return null;
}

function index(o) {
  for (const dx of [-W, 0, W]) for (const dy of [-H, 0, H]) {
    for (const cell of cellsFor(o, dx, dy)) {
      if (!grid.has(cell)) grid.set(cell, []);
      grid.get(cell).push(o);
    }
  }
}

function textWidth(text, fontSize) {
  const units = Array.from(text).reduce((sum, char) => {
    if (/\s/.test(char)) return sum + .31;
    if (/[ilI.,:;!'|]/.test(char)) return sum + .31;
    if (/[(){}\[\]]/.test(char)) return sum + .38;
    if (/[0-9]/.test(char)) return sum + .54;
    if (/[+−=<>≤≥±∫∑√∂∇∞πφθαλμ]/.test(char)) return sum + .66;
    return sum + .57;
  }, 0);
  return units * fontSize * 1.04;
}

function makeCandidate(kind, index) {
  const isDiagram = kind === 'large';
  if (isDiagram) {
    const w = int(98, 156), h = int(80, 142);
    return {kind, index, family: `diagram-${index % 32}`, diagram: true, w, h, markup: diagram(index, w, h)};
  }

  const expression = makeFormula();
  const fontSize = kind === 'medium' ? int(15, 19) : int(10, 13);
  const contentWidth = textWidth(expression.text, fontSize);
  const w = Math.max(48, contentWidth + 8);
  const h = fontSize * 1.72;
  const spacing = (rand() * .22 - .08).toFixed(2);
  const markup = `<text x="4" y="${fontSize}" font-size="${fontSize}" letter-spacing="${spacing}" textLength="${contentWidth.toFixed(1)}" lengthAdjust="spacingAndGlyphs">${esc(expression.text)}</text>`;
  return {kind, index, family: expression.family, text: expression.text, fontSize, w, h, markup};
}

function makeFiller(index) {
  const x = pick(VARS), y = pick(VARS.filter(v => v !== x));
  const text = pick([
    () => `${x} = ${int(2, 19)}${y}`,
    () => `Δ${x} → 0`,
    () => `∠${pick(['α', 'β', 'γ', 'θ'])} = ${int(20, 160)}°`,
    () => `${x} ∈ ℝ`,
    () => `π ≈ 3.14`,
    () => `λ${int(1, 5)} > 0`,
    () => `d${x}/d${y}`,
    () => `${x}² + ${y}²`,
    () => `∴ ${x} = ${int(1, 9)}`,
  ])();
  const fontSize = int(9, 11);
  const contentWidth = textWidth(text, fontSize);
  const w = Math.max(38, contentWidth + 8);
  const h = fontSize * 1.72;
  const markup = `<text x="4" y="${fontSize}" font-size="${fontSize}" letter-spacing="0.1" textLength="${contentWidth.toFixed(1)}" lengthAdjust="spacingAndGlyphs">${esc(text)}</text>`;
  return {kind: 'small', index, family: 'notation-filler', text, fontSize, w, h, markup};
}

const candidates = [
  ...Array.from({length: 60}, (_, i) => makeCandidate('large', i)),
  ...Array.from({length: 120}, (_, i) => makeCandidate('medium', i)),
  ...Array.from({length: 220}, (_, i) => makeCandidate('small', i)),
  ...Array.from({length: 80}, (_, i) => makeFiller(i)),
];

for (const candidate of candidates) {
  const rotation = (rand() - .5) * (candidate.diagram ? 28 : 18);
  const radians = Math.abs(rotation) * Math.PI / 180;
  const bw = Math.abs(candidate.w * Math.cos(radians)) + Math.abs(candidate.h * Math.sin(radians));
  const bh = Math.abs(candidate.w * Math.sin(radians)) + Math.abs(candidate.h * Math.cos(radians));
  let item;
  for (let attempt = 0; attempt < 3500; attempt++) {
    item = {...candidate, x: rand() * W, y: rand() * H, bw, bh, rotation};
    const reason = collides(item);
    if (!reason) break;
    if (reason === 'similarity') rejectedSimilarityCandidates++;
    else rejectedCollisionCandidates++;
    item = null;
  }
  if (!item) continue;
  if (placed.length >= TARGET) break;

  item.color = pick(COLORS);
  item.opacity = (.52 + rand() * .28).toFixed(2);
  item.strokeWidth = (1.25 + rand() * .45).toFixed(2);
  placed.push(item);
  index(item);
}

function render(item, dx = 0, dy = 0) {
  const x = item.x + dx - item.w / 2;
  const y = item.y + dy - item.h / 2;
  return `<g transform="translate(${x.toFixed(1)} ${y.toFixed(1)}) rotate(${item.rotation.toFixed(1)} ${item.w / 2} ${item.h / 2})" stroke="${item.color}" fill="${item.color}" opacity="${item.opacity}" stroke-width="${item.strokeWidth}" stroke-linecap="round" stroke-linejoin="round">${item.markup}</g>`;
}

let art = '';
for (const item of placed) {
  for (const dx of [-W, 0, W]) for (const dy of [-H, 0, H]) {
    const x = item.x + dx, y = item.y + dy;
    if (x + item.bw / 2 >= 0 && x - item.bw / 2 <= W && y + item.bh / 2 >= 0 && y - item.bh / 2 <= H) {
      art += render(item, dx, dy);
    }
  }
}

const svgOpen = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">`;
const artGroup = `<g font-family="Segoe Print, Bradley Hand, Comic Sans MS, cursive" font-weight="400">${art}</g>`;
const svg = `${svgOpen}<rect width="${W}" height="${H}" fill="#050B07"/>${artGroup}</svg>`;
const patternSvg = `${svgOpen}${artGroup}</svg>`;
const out = path.join(__dirname, '..', 'webapp', 'assets');
fs.mkdirSync(out, {recursive: true});
fs.writeFileSync(path.join(out, 'math-wallpaper.svg'), svg);
fs.writeFileSync(path.join(out, 'math-wallpaper-pattern.svg'), patternSvg);

function boxClearance(a, b) {
  const dx = wrappedDistance(a.x, b.x, W);
  const dy = wrappedDistance(a.y, b.y, H);
  const overlapX = (a.bw + b.bw) / 2 - dx;
  const overlapY = (a.bh + b.bh) / 2 - dy;
  if (overlapX > 0 && overlapY > 0) return -Math.min(overlapX, overlapY);
  return Math.hypot(Math.max(0, -overlapX), Math.max(0, -overlapY));
}

let intersections = 0;
let minSpacing = Infinity;
let minSameFamilyDistance = Infinity;
let nearestTotal = 0;
for (let i = 0; i < placed.length; i++) {
  let nearest = Infinity;
  for (let j = 0; j < placed.length; j++) {
    if (i === j) continue;
    if (j > i && placed[i].family === placed[j].family) {
      const dx = wrappedDistance(placed[i].x, placed[j].x, W);
      const dy = wrappedDistance(placed[i].y, placed[j].y, H);
      minSameFamilyDistance = Math.min(minSameFamilyDistance, Math.hypot(dx, dy));
    }
    const clearance = boxClearance(placed[i], placed[j]);
    if (clearance < 0) intersections++;
    nearest = Math.min(nearest, clearance);
  }
  minSpacing = Math.min(minSpacing, nearest);
  nearestTotal += nearest;
}

const usedFamilies = new Set(placed.map(item => item.family));
const formulaVariants = new Set(placed.filter(item => !item.diagram).map(item => item.text));
const diagramVariants = new Set(placed.filter(item => item.diagram).map(item => item.markup));
const compositions = new Set([...formulaVariants, ...diagramVariants]);
const report = {
  tile: `${W}x${H}`,
  target: TARGET,
  placed: placed.length,
  distribution: Object.fromEntries(['large', 'medium', 'small'].map(kind => [kind, placed.filter(item => item.kind === kind).length])),
  templateFamilies: usedFamilies.size,
  formulaTemplateFamilies: new Set(placed.filter(item => !item.diagram).map(item => item.family)).size,
  diagramTemplateFamilies: new Set(placed.filter(item => item.diagram).map(item => item.family)).size,
  visuallyDistinctVariants: compositions.size,
  uniqueFormulaCompositions: formulaVariants.size,
  uniqueDiagramCompositions: diagramVariants.size,
  rejectedCollisionCandidates,
  rejectedSimilarityCandidates,
  detectedIntersections: Math.floor(intersections / 2),
  collisionFree: intersections === 0,
  minimumSpacing: placed.length > 1 ? +minSpacing.toFixed(2) : null,
  averageNearestSpacing: placed.length > 1 ? +(nearestTotal / placed.length).toFixed(2) : null,
  nearDuplicateRadius: REPEAT_RADIUS,
  minimumSameFamilyCenterDistance: Number.isFinite(minSameFamilyDistance) ? +minSameFamilyDistance.toFixed(2) : null,
  estimatedAabbCoveragePercent: +(placed.reduce((sum, item) => sum + item.bw * item.bh, 0) / (W * H) * 100).toFixed(2),
  collisionBounds: 'rotated AABB with SVG textLength and periodic checks against neighboring tile offsets',
  densityTargetMet: placed.length === TARGET,
  distinctVariantTargetMet: compositions.size >= 200,
  crossBoundaryCollisionChecks: true,
  seamlessEdgeCopies: true,
};
fs.writeFileSync(path.join(out, 'math-wallpaper-report.json'), `${JSON.stringify(report, null, 2)}\n`);
console.log(JSON.stringify(report, null, 2));
