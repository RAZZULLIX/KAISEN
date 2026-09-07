# Copyright (c) 2026 LABORATORI RAZZULLIX - MIT License. See LICENSE.
"""Project factory — programmatic generation of algorithm × language projects
for long campaigns.

Each generated project is a complete, self-contained evolution target:

    projects/<algo>-<lang>/
        original.<ext>          naive baseline (the starting candidate)
        fuzz_cases.json         SEEDED inputs + reference outputs (fuzzlib)
        harness/build.py        language build step
        harness/fuzz_verify.py  correctness gate (differential vs reference)
        harness/score.py        timing harness on a fixed workload

Guarantees enforced at factory time (a project that fails any of these is
NOT registered):
  1. spec passes validate_spec;
  2. the baseline BUILDS with its own build step;
  3. the baseline PASSES its full fuzz gate (it IS the reference behavior);
  4. the baseline SCORES (score.py exits 0 and emits time_ms).

Reference outputs are computed once per algorithm (they are language-
independent), from a trusted Python reference — never from the baselines
themselves.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import fuzzlib as F
from .projects import validate_spec

FRAMEWORK_ROOT = Path(__file__).resolve().parent.parent
SHARED_FUZZ_VERIFY = FRAMEWORK_ROOT / "kaisen" / "templates" / "_shared" / "fuzz_verify.py"

MOD = 1_000_000_007

# The factory covers EVERY language in the framework's registry (single
# source of truth — kaisen/languages.py). Languages whose toolchain or
# interpreter is missing on this machine are skipped at registration by
# create_all's preflight and reported, never shipped broken.
from . import languages as _LANGS

LANG_EXT = {k: str(v["ext"]).lstrip(".") for k, v in _LANGS.LANGUAGES.items()}
LANG_LABEL = {"c": "C", "cpp": "C++", "cuda": "CUDA C++", "python": "Python",
              "java": "Java", "javascript": "JavaScript", "typescript": "TypeScript",
              "csharp": "C#", "go": "Go", "rust": "Rust", "kotlin": "Kotlin",
              "swift": "Swift", "php": "PHP", "ruby": "Ruby", "r": "R",
              "zig": "Zig", "scala": "Scala", "dart": "Dart",
              "haskell": "Haskell", "lua": "Lua", "perl": "Perl",
              "shell": "Shell (bash)", "d": "D"}
# Interpreted languages: acceptable interpreter binaries (first hit wins).
INTERPRETERS = {
    "python": ("python3",),
    "javascript": ("node",),
    "php": ("php",),
    "ruby": ("ruby",),
    "r": ("Rscript", "R"),
    "lua": ("lua", "lua5.4", "lua5.3", "luajit"),
    "perl": ("perl",),
    "shell": ("bash", "sh"),
}


# --------------------------------------------------------------------------- #
# deterministic workloads (embedded verbatim into each generated score.py)
# --------------------------------------------------------------------------- #

def _int_stream(seed: int, n: int, lo: int, hi: int) -> List[List[str]]:
    rng = random.Random(seed)
    return [[str(rng.randint(lo, hi))] for _ in range(n)]


def _pair_stream(seed: int, n: int, alo: int, ahi: int,
                 blo: int, bhi: int) -> List[List[str]]:
    rng = random.Random(seed)
    return [[str(rng.randint(alo, ahi)), str(rng.randint(blo, bhi))]
            for _ in range(n)]


def _str_stream(seed: int, n: int, maxlen: int = 80) -> List[List[str]]:
    rng = random.Random(seed)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    lengths = [1, 2, 3, 5, 8, 13, 21, 40, maxlen]
    out: List[List[str]] = []
    seen = set()
    while len(out) < n:
        s = "".join(rng.choice(alphabet) for _ in range(rng.choice(lengths)))
        if s in seen:
            continue
        seen.add(s)
        out.append([s])
    return out

def _pair_str_stream(seed: int, n: int, maxlen: int = 40) -> List[List[str]]:
    rng = random.Random(seed)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    lengths = [1, 2, 3, 5, 8, 13, 21, maxlen]
    out: List[List[str]] = []
    seen = set()
    while len(out) < n:
        a = "".join(rng.choice(alphabet) for _ in range(rng.choice(lengths)))
        b = "".join(rng.choice(alphabet) for _ in range(rng.choice(lengths)))
        if (a, b) in seen:
            continue
        seen.add((a, b))
        out.append([a, b])
    return out


def _triple_stream(seed: int, n: int, alo: int, ahi: int,
                   blo: int, bhi: int, mlo: int, mhi: int) -> List[List[str]]:
    rng = random.Random(seed)
    return [[str(rng.randint(alo, ahi)), str(rng.randint(blo, bhi)),
             str(rng.randint(mlo, mhi))] for _ in range(n)]


def _intlist_stream(seed: int, n: int, max_len: int = 400) -> List[List[str]]:
    rng = random.Random(seed)
    out: List[List[str]] = []
    seen = set()
    while len(out) < n:
        k = rng.choice([1, 2, 5, 13, 40, max_len // 2, max_len])
        s = " ".join(str(rng.randint(-10 ** 6, 10 ** 6)) for _ in range(k))
        if s in seen:
            continue
        seen.add(s)
        out.append([s])
    return out


# --------------------------------------------------------------------------- #
# reference implementations (Python, factory-time only — define ground truth)
# --------------------------------------------------------------------------- #

_REF = {
    "prime-count": '''\
import sys

def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if n < 2:
        print(0)
        return
    limit = n - 1
    sieve = bytearray([1]) * (limit + 1)
    sieve[0] = sieve[1] = 0
    p = 2
    while p * p <= limit:
        if sieve[p]:
            sieve[p * p::p] = bytearray(len(range(p * p, limit + 1, p)))
        p += 1
    print(sum(sieve))

main()
''',
    "popcount": '''\
import sys
x = int(sys.argv[1]) if len(sys.argv) > 1 else 0
print(bin(x).count("1"))
''',
    "gcd": '''\
import math, sys
a = int(sys.argv[1]); b = int(sys.argv[2])
print(math.gcd(a, b))
''',
    "fib-mod": f'''\
import sys

def fib(n):
    def f(k):
        if k == 0:
            return (0, 1)
        a, b = f(k >> 1)
        c = a * (2 * b - a) % {MOD}
        d = (a * a + b * b) % {MOD}
        if k & 1:
            return (d, (c + d) % {MOD})
        return (c, d)
    return f(n)[0]

n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
print(fib(n))
''',
    "num-divisors": '''\
import sys
n = int(sys.argv[1])
c, d = 0, 1
while d * d <= n:
    if n % d == 0:
        c += 1 if d * d == n else 2
    d += 1
print(c)
''',
    "collatz-steps": '''\
import sys
n = int(sys.argv[1])
s = 0
while n > 1:
    n = n // 2 if n % 2 == 0 else 3 * n + 1
    s += 1
print(s)
''',
    "sum-range": f'''\
import sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
print((n * (n + 1) // 2) % {MOD})
''',
    "reverse-str": '''\
import sys
s = sys.argv[1] if len(sys.argv) > 1 else ""
print(s[::-1])
''',
    "is-palindrome": '''\
import sys
s = sys.argv[1] if len(sys.argv) > 1 else ""
print(1 if s == s[::-1] else 0)
''',
    "rle": '''\
import re, sys
s = sys.argv[1] if len(sys.argv) > 1 else ""
print("".join(m.group(0)[0] + str(len(m.group(0)))
              for m in re.finditer(r"(.)\\1*", s)))
''',
    "is-prime": '''\
import sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
if n < 2:
    print(0)
else:
    d, ok = 2, True
    while d * d <= n and ok:
        if n % d == 0:
            ok = False
        d += 1
    print(1 if ok else 0)
''',
    "int-sqrt": '''\
import math, sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
print(math.isqrt(n))
''',
    "digital-root": '''\
import sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
while n >= 10:
    n = sum(int(c) for c in str(n))
print(n)
''',
    "trailing-zeros": '''\
import sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
c = 0
while n and n % 2 == 0:
    c += 1
    n //= 2
print(c)
''',
    "omega": '''\
import sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
c, d = 0, 2
while d * d <= n:
    while n % d == 0:
        c += 1
        n //= d
    d += 1
if n > 1:
    c += 1
print(c)
''',
    "nth-prime": '''\
import sys
k = int(sys.argv[1]) if len(sys.argv) > 1 else 0
def is_p(x):
    if x < 2:
        return False
    d = 2
    while d * d <= x:
        if x % d == 0:
            return False
        d += 1
    return True
c, x = 0, 1
while c < k:
    x += 1
    if is_p(x):
        c += 1
print(x)
''',
    "levenshtein": '''\
import sys
a = sys.argv[1] if len(sys.argv) > 1 else ""
b = sys.argv[2] if len(sys.argv) > 2 else ""
prev = list(range(len(b) + 1))
for i, ca in enumerate(a, 1):
    cur = [i]
    for j, cb in enumerate(b, 1):
        cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
    prev = cur
print(prev[-1])
''',
    "lcs-length": '''\
import sys
a = sys.argv[1] if len(sys.argv) > 1 else ""
b = sys.argv[2] if len(sys.argv) > 2 else ""
prev = [0] * (len(b) + 1)
for ca in a:
    cur = [0]
    for j, cb in enumerate(b, 1):
        cur.append(prev[j - 1] + 1 if ca == cb else max(prev[j], cur[j - 1]))
    prev = cur
print(prev[-1])
''',
    "lpal-len": '''\
import sys
s = sys.argv[1] if len(sys.argv) > 1 else ""
best = 0
for i in range(len(s)):
    for j in range(i, len(s)):
        t = s[i:j + 1]
        if t == t[::-1] and len(t) > best:
            best = len(t)
print(best)
''',
    "kmp-prefix": '''\
import sys
s = sys.argv[1] if len(sys.argv) > 1 else ""
pi = [0] * len(s)
for i in range(1, len(s)):
    j = pi[i - 1]
    while j > 0 and s[i] != s[j]:
        j = pi[j - 1]
    if s[i] == s[j]:
        j += 1
    pi[i] = j
print(" ".join(map(str, pi)))
''',
    "caesar-shift": '''\
import sys
s = sys.argv[1] if len(sys.argv) > 1 else ""
out = []
for ch in s:
    if "a" <= ch <= "z":
        out.append(chr(ord("a") + (ord(ch) - ord("a") + 3) % 26))
    elif "A" <= ch <= "Z":
        out.append(chr(ord("A") + (ord(ch) - ord("A") + 3) % 26))
    else:
        out.append(ch)
print("".join(out))
''',
    "happy-steps": '''\
import sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
seen, s = set(), 0
while n != 1:
    if n in seen:
        break
    seen.add(n)
    n = sum(int(c) ** 2 for c in str(n))
    s += 1
print(s)
''',
    "modexp": '''\
import sys
a = int(sys.argv[1]) if len(sys.argv) > 1 else 0
b = int(sys.argv[2]) if len(sys.argv) > 2 else 0
m = int(sys.argv[3]) if len(sys.argv) > 3 else 1
print(pow(a, b, m))
''',
    "max-subarray": '''\
import sys
nums = [int(x) for x in (sys.argv[1].split() if len(sys.argv) > 1 else [])]
if not nums:
    print(0)
    raise SystemExit
best = cur = nums[0]
for x in nums[1:]:
    cur = max(x, cur + x)
    best = max(best, cur)
print(best)
''',
    "count-inversions": '''\
import sys
nums = [int(x) for x in (sys.argv[1].split() if len(sys.argv) > 1 else [])]
c = 0
for i in range(len(nums)):
    for j in range(i + 1, len(nums)):
        if nums[i] > nums[j]:
            c += 1
print(c)
''',
}


# --------------------------------------------------------------------------- #
# naive baselines per language (the starting candidates the AI evolves)
# --------------------------------------------------------------------------- #

_BASELINES: Dict[str, Dict[str, str]] = {
    "prime-count": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
static int is_prime(long x) {
    if (x < 2) return 0;
    for (long d = 2; d * d <= x; d++) if (x % d == 0) return 0;
    return 1;
}
int main(int argc, char **argv) {
    long n = argc > 1 ? atol(argv[1]) : 0;
    long c = 0;
    for (long i = 2; i < n; i++) c += is_prime(i);
    printf("%ld\\n", c);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys

def is_prime(x):
    if x < 2:
        return False
    d = 2
    while d * d <= x:
        if x % d == 0:
            return False
        d += 1
    return True

n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
print(sum(1 for i in range(2, n) if is_prime(i)))
''',
        "rust": '''\
fn is_prime(x: u64) -> bool {
    if x < 2 { return false; }
    let mut d = 2u64;
    while d * d <= x {
        if x % d == 0 { return false; }
        d += 1;
    }
    true
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let n: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let mut c = 0u64;
    for i in 2..n {
        if is_prime(i) { c += 1; }
    }
    println!("{}", c);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

func isPrime(x int64) bool {
\tif x < 2 { return false }
\tfor d := int64(2); d*d <= x; d++ {
\t\tif x%d == 0 { return false }
\t}
\treturn true
}

func main() {
\tn := int64(0)
\tif len(os.Args) > 1 { n, _ = strconv.ParseInt(os.Args[1], 10, 64) }
\tvar c int64
\tfor i := int64(2); i < n; i++ {
\t\tif isPrime(i) { c++ }
\t}
\tfmt.Println(c)
}
''',
    },
    "popcount": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    unsigned long x = argc > 1 ? strtoul(argv[1], 0, 10) : 0;
    unsigned c = 0;
    while (x) { c += x & 1UL; x >>= 1; }
    printf("%u\\n", c);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
x = int(sys.argv[1]) if len(sys.argv) > 1 else 0
c = 0
while x:
    c += x & 1
    x >>= 1
print(c)
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut x: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let mut c = 0u32;
    while x != 0 {
        c += (x & 1) as u32;
        x >>= 1;
    }
    println!("{}", c);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

func main() {
\tx := uint64(0)
\tif len(os.Args) > 1 { x, _ = strconv.ParseUint(os.Args[1], 10, 64) }
\tvar c uint32
\tfor x != 0 {
\t\tc += uint32(x & 1)
\t\tx >>= 1
\t}
\tfmt.Println(c)
}
''',
    },
    "gcd": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    long a = atol(argv[1]), b = atol(argv[2]);
    while (b) { long t = a % b; a = b; b = t; }
    printf("%ld\\n", a);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
a, b = int(sys.argv[1]), int(sys.argv[2])
while b:
    a, b = b, a % b
print(a)
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut a: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let mut b: u64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(0);
    while b != 0 {
        let t = a % b;
        a = b;
        b = t;
    }
    println!("{}", a);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

func main() {
\ta, _ := strconv.ParseUint(os.Args[1], 10, 64)
\tb, _ := strconv.ParseUint(os.Args[2], 10, 64)
\tfor b != 0 {
\t\ta, b = b, a%b
\t}
\tfmt.Println(a)
}
''',
    },
    "fib-mod": {
        "c": f'''\
#include <stdio.h>
#include <stdlib.h>
#define MOD {MOD}L
int main(int argc, char **argv) {{
    long n = argc > 1 ? atol(argv[1]) : 0;
    long a = 0, b = 1;
    for (long i = 0; i < n; i++) {{ long t = a + b; a = b; b = t % MOD; }}
    printf("%ld\\n", a);
    return 0;
}}
''',
        "python": f'''\
#!/usr/bin/env python3
import sys
MOD = {MOD}
n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
a, b = 0, 1
for _ in range(n):
    a, b = b, (a + b) % MOD
print(a)
''',
        "rust": f'''\
const MOD: u64 = {MOD};

fn main() {{
    let args: Vec<String> = std::env::args().collect();
    let n: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let (mut a, mut b) = (0u64, 1u64);
    for _ in 0..n {{
        let t = (a + b) % MOD;
        a = b;
        b = t;
    }}
    println!("{{}}", a);
}}
''',
        "go": f'''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

const mod = {MOD}

func main() {{
\tn := uint64(0)
\tif len(os.Args) > 1 {{ n, _ = strconv.ParseUint(os.Args[1], 10, 64) }}
\ta, b := uint64(0), uint64(1)
\tfor i := uint64(0); i < n; i++ {{
\t\ta, b = b, (a+b)%mod
\t}}
\tfmt.Println(a)
}}
''',
    },
    "num-divisors": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    long n = atol(argv[1]);
    long c = 0;
    for (long d = 1; d * d <= n; d++)
        if (n % d == 0) c += (d * d == n) ? 1 : 2;
    printf("%ld\\n", c);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
n = int(sys.argv[1])
c, d = 0, 1
while d * d <= n:
    if n % d == 0:
        c += 1 if d * d == n else 2
    d += 1
print(c)
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let n: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let mut c = 0u64;
    let mut d = 1u64;
    while d * d <= n {
        if n % d == 0 { c += if d * d == n { 1 } else { 2 }; }
        d += 1;
    }
    println!("{}", c);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

func main() {
\tn, _ := strconv.ParseUint(os.Args[1], 10, 64)
\tvar c uint64
\tfor d := uint64(1); d*d <= n; d++ {
\t\tif n%d == 0 {
\t\t\tif d*d == n { c += 1 } else { c += 2 }
\t\t}
\t}
\tfmt.Println(c)
}
''',
    },
    "collatz-steps": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    unsigned long long n = strtoull(argv[1], 0, 10);
    unsigned long long s = 0;
    while (n > 1) { if (n % 2 == 0) n /= 2; else n = 3 * n + 1; s++; }
    printf("%llu\\n", s);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
n = int(sys.argv[1])
s = 0
while n > 1:
    n = n // 2 if n % 2 == 0 else 3 * n + 1
    s += 1
print(s)
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut n: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let mut s = 0u64;
    while n > 1 {
        n = if n % 2 == 0 { n / 2 } else { 3 * n + 1 };
        s += 1;
    }
    println!("{}", s);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

func main() {
\tn, _ := strconv.ParseUint(os.Args[1], 10, 64)
\tvar s uint64
\tfor n > 1 {
\t\tif n%2 == 0 { n /= 2 } else { n = 3*n + 1 }
\t\ts++
\t}
\tfmt.Println(s)
}
''',
    },
    "sum-range": {
        "c": f'''\
#include <stdio.h>
#include <stdlib.h>
#define MOD {MOD}L
int main(int argc, char **argv) {{
    long long n = argc > 1 ? atoll(argv[1]) : 0;
    long long s = 0;
    for (long long i = 1; i <= n; i++) s = (s + i) % MOD;
    printf("%lld\\n", s);
    return 0;
}}
''',
        "python": f'''\
#!/usr/bin/env python3
import sys
MOD = {MOD}
n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
s = 0
for i in range(1, n + 1):
    s = (s + i) % MOD
print(s)
''',
        "rust": f'''\
const MOD: u64 = {MOD};

fn main() {{
    let args: Vec<String> = std::env::args().collect();
    let n: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let mut s = 0u64;
    for i in 1..=n {{ s = (s + i) % MOD; }}
    println!("{{}}", s);
}}
''',
        "go": f'''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

const mod = {MOD}

func main() {{
\tn := uint64(0)
\tif len(os.Args) > 1 {{ n, _ = strconv.ParseUint(os.Args[1], 10, 64) }}
\tvar s uint64
\tfor i := uint64(1); i <= n; i++ {{
\t\ts = (s + i) % mod
\t}}
\tfmt.Println(s)
}}
''',
    },
    "reverse-str": {
        "c": '''\
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
    if (argc < 2) { puts(""); return 0; }
    const char *s = argv[1];
    size_t l = strlen(s);
    for (size_t i = l; i-- > 0;) putchar(s[i]);
    putchar('\\n');
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
s = sys.argv[1] if len(sys.argv) > 1 else ""
print("".join(reversed(s)))
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let s = args.get(1).cloned().unwrap_or_default();
    println!("{}", s.chars().rev().collect::<String>());
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
)

func main() {
\ts := ""
\tif len(os.Args) > 1 { s = os.Args[1] }
\tr := []rune(s)
\tfor i, j := 0, len(r)-1; i < j; i, j = i+1, j-1 {
\t\tr[i], r[j] = r[j], r[i]
\t}
\tfmt.Println(string(r))
}
''',
    },
    "is-palindrome": {
        "c": '''\
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
    const char *s = argc > 1 ? argv[1] : "";
    size_t l = strlen(s);
    int ok = 1;
    for (size_t i = 0, j = l; i < j; i++, j--)
        if (s[i] != s[j - 1]) { ok = 0; break; }
    puts(ok ? "1" : "0");
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
s = sys.argv[1] if len(sys.argv) > 1 else ""
i, j, ok = 0, len(s) - 1, True
while i < j:
    if s[i] != s[j]:
        ok = False
        break
    i += 1
    j -= 1
print(1 if ok else 0)
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let s: Vec<char> = args.get(1).map(|s| s.chars().collect()).unwrap_or_default();
    let mut ok = true;
    let mut i = 0usize;
    let mut j = s.len();
    while i < j {
        j -= 1;
        if s[i] != s[j] { ok = false; break; }
        i += 1;
    }
    println!("{}", if ok { 1 } else { 0 });
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
)

func main() {
\ts := ""
\tif len(os.Args) > 1 { s = os.Args[1] }
\tr := []rune(s)
\tok := true
\tfor i, j := 0, len(r)-1; i < j; i, j = i+1, j-1 {
\t\tif r[i] != r[j] { ok = false; break }
\t}
\tif ok { fmt.Println(1) } else { fmt.Println(0) }
}
''',
    },
    "rle": {
        "c": '''\
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
    if (argc < 2) { putchar('\\n'); return 0; }
    const char *s = argv[1];
    size_t l = strlen(s), i = 0;
    while (i < l) {
        size_t j = i;
        while (j < l && s[j] == s[i]) j++;
        putchar(s[i]);
        printf("%zu", j - i);
        i = j;
    }
    putchar('\\n');
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
s = sys.argv[1] if len(sys.argv) > 1 else ""
out, i, n = [], 0, len(s)
while i < n:
    j = i
    while j < n and s[j] == s[i]:
        j += 1
    out.append(s[i] + str(j - i))
    i = j
print("".join(out))
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let s: Vec<char> = args.get(1).map(|s| s.chars().collect()).unwrap_or_default();
    let mut out = String::new();
    let mut i = 0usize;
    while i < s.len() {
        let mut j = i;
        while j < s.len() && s[j] == s[i] { j += 1; }
        out.push(s[i]);
        out.push_str(&(j - i).to_string());
        i = j;
    }
    println!("{}", out);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
)

func main() {
\ts := ""
\tif len(os.Args) > 1 { s = os.Args[1] }
\tr := []rune(s)
\tvar out []rune
\tfor i := 0; i < len(r); {
\t\tj := i
\t\tfor j < len(r) && r[j] == r[i] { j++ }
\t\tout = append(out, r[i])
\t\tout = append(out, []rune(fmt.Sprintf("%d", j-i))...)
\t\ti = j
\t}
\tfmt.Println(string(out))
}
''',
    },
    "is-prime": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    long n = argc > 1 ? atol(argv[1]) : 0;
    if (n < 2) { printf("0\\n"); return 0; }
    for (long d = 2; d * d <= n; d++)
        if (n % d == 0) { printf("0\\n"); return 0; }
    printf("1\\n");
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
if n < 2:
    print(0)
else:
    d, ok = 2, True
    while d * d <= n and ok:
        if n % d == 0:
            ok = False
        d += 1
    print(1 if ok else 0)
''',
        "rust": '''\
fn is_prime(x: u64) -> bool {
    if x < 2 { return false; }
    let mut d = 2u64;
    while d * d <= x {
        if x % d == 0 { return false; }
        d += 1;
    }
    true
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let n: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    println!("{}", if is_prime(n) { 1 } else { 0 });
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

func isPrime(x int64) bool {
\tif x < 2 { return false }
\tfor d := int64(2); d*d <= x; d++ {
\t\tif x%d == 0 { return false }
\t}
\treturn true
}

func main() {
\tn := int64(0)
\tif len(os.Args) > 1 { n, _ = strconv.ParseInt(os.Args[1], 10, 64) }
\tif isPrime(n) { fmt.Println(1) } else { fmt.Println(0) }
}
''',
    },
    "int-sqrt": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    long n = argc > 1 ? atol(argv[1]) : 0;
    long x = 0;
    while ((x + 1) * (x + 1) <= n) x++;
    printf("%ld\\n", x);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
x = 0
while (x + 1) * (x + 1) <= n:
    x += 1
print(x)
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let n: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let mut x = 0u64;
    while (x + 1) * (x + 1) <= n { x += 1; }
    println!("{}", x);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

func main() {
\tn := int64(0)
\tif len(os.Args) > 1 { n, _ = strconv.ParseInt(os.Args[1], 10, 64) }
\tx := int64(0)
\tfor (x+1)*(x+1) <= n { x++ }
\tfmt.Println(x)
}
''',
    },
    "digital-root": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    long n = argc > 1 ? atol(argv[1]) : 0;
    while (n >= 10) {
        long s = 0;
        while (n) { s += n % 10; n /= 10; }
        n = s;
    }
    printf("%ld\\n", n);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
while n >= 10:
    s, m = 0, n
    while m:
        s += m % 10
        m //= 10
    n = s
print(n)
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut n: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    while n >= 10 {
        let mut s = 0u64;
        let mut m = n;
        while m > 0 { s += m % 10; m /= 10; }
        n = s;
    }
    println!("{}", n);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

func main() {
\tn := int64(0)
\tif len(os.Args) > 1 { n, _ = strconv.ParseInt(os.Args[1], 10, 64) }
\tfor n >= 10 {
\t\tvar s int64
\t\tm := n
\t\tfor m > 0 { s += m % 10; m /= 10 }
\t\tn = s
\t}
\tfmt.Println(n)
}
''',
    },
    "trailing-zeros": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    long n = argc > 1 ? atol(argv[1]) : 0;
    long c = 0;
    while (n != 0 && n % 2 == 0) { c++; n /= 2; }
    printf("%ld\\n", c);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
c = 0
while n and n % 2 == 0:
    c += 1
    n //= 2
print(c)
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut n: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let mut c = 0u64;
    while n != 0 && n % 2 == 0 { c += 1; n /= 2; }
    println!("{}", c);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

func main() {
\tn := int64(0)
\tif len(os.Args) > 1 { n, _ = strconv.ParseInt(os.Args[1], 10, 64) }
\tc := int64(0)
\tfor n != 0 && n%2 == 0 { c++; n /= 2 }
\tfmt.Println(c)
}
''',
    },
    "omega": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    long n = argc > 1 ? atol(argv[1]) : 1;
    long c = 0, d = 2;
    while (d * d <= n) {
        while (n % d == 0) { c++; n /= d; }
        d++;
    }
    if (n > 1) c++;
    printf("%ld\\n", c);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
c, d = 0, 2
while d * d <= n:
    while n % d == 0:
        c += 1
        n //= d
    d += 1
if n > 1:
    c += 1
print(c)
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut n: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(1);
    let mut c = 0u64;
    let mut d = 2u64;
    while d * d <= n {
        while n % d == 0 { c += 1; n /= d; }
        d += 1;
    }
    if n > 1 { c += 1; }
    println!("{}", c);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

func main() {
\tn := int64(1)
\tif len(os.Args) > 1 { n, _ = strconv.ParseInt(os.Args[1], 10, 64) }
\tc, d := int64(0), int64(2)
\tfor d*d <= n {
\t\tfor n%d == 0 { c++; n /= d }
\t\td++
\t}
\tif n > 1 { c++ }
\tfmt.Println(c)
}
''',
    },
    "nth-prime": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
static int is_p(long x) {
    if (x < 2) return 0;
    for (long d = 2; d * d <= x; d++)
        if (x % d == 0) return 0;
    return 1;
}
int main(int argc, char **argv) {
    long k = argc > 1 ? atol(argv[1]) : 0;
    long c = 0, x = 1;
    while (c < k) { x++; if (is_p(x)) c++; }
    printf("%ld\\n", x);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
k = int(sys.argv[1]) if len(sys.argv) > 1 else 0
def is_p(x):
    if x < 2:
        return False
    d = 2
    while d * d <= x:
        if x % d == 0:
            return False
        d += 1
    return True
c, x = 0, 1
while c < k:
    x += 1
    if is_p(x):
        c += 1
print(x)
''',
        "rust": '''\
fn is_p(x: u64) -> bool {
    if x < 2 { return false; }
    let mut d = 2u64;
    while d * d <= x {
        if x % d == 0 { return false; }
        d += 1;
    }
    true
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let k: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let mut c = 0u64;
    let mut x = 1u64;
    while c < k { x += 1; if is_p(x) { c += 1; } }
    println!("{}", x);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

func isP(x int64) bool {
\tif x < 2 { return false }
\tfor d := int64(2); d*d <= x; d++ {
\t\tif x%d == 0 { return false }
\t}
\treturn true
}

func main() {
\tk := int64(0)
\tif len(os.Args) > 1 { k, _ = strconv.ParseInt(os.Args[1], 10, 64) }
\tc, x := int64(0), int64(1)
\tfor c < k {
\t\tx++
\t\tif isP(x) { c++ }
\t}
\tfmt.Println(x)
}
''',
    },
    "levenshtein": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(int argc, char **argv) {
    const char *a = argc > 1 ? argv[1] : "";
    const char *b = argc > 2 ? argv[2] : "";
    size_t la = strlen(a), lb = strlen(b);
    int *prev = malloc((lb + 1) * sizeof(int));
    for (size_t j = 0; j <= lb; j++) prev[j] = (int)j;
    for (size_t i = 1; i <= la; i++) {
        int *cur = malloc((lb + 1) * sizeof(int));
        cur[0] = (int)i;
        for (size_t j = 1; j <= lb; j++) {
            int sub = prev[j - 1] + (a[i - 1] != b[j - 1]);
            int del = prev[j] + 1, ins = cur[j - 1] + 1;
            cur[j] = del < ins ? (del < sub ? del : sub) : (ins < sub ? ins : sub);
        }
        free(prev);
        prev = cur;
    }
    printf("%d\\n", prev[lb]);
    free(prev);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
a = sys.argv[1] if len(sys.argv) > 1 else ""
b = sys.argv[2] if len(sys.argv) > 2 else ""
prev = list(range(len(b) + 1))
for i, ca in enumerate(a, 1):
    cur = [i]
    for j, cb in enumerate(b, 1):
        cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
    prev = cur
print(prev[-1])
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let a: String = args.get(1).cloned().unwrap_or_default();
    let b: String = args.get(2).cloned().unwrap_or_default();
    let aa: Vec<char> = a.chars().collect();
    let ba: Vec<char> = b.chars().collect();
    let (la, lb) = (aa.len(), ba.len());
    let mut prev: Vec<i32> = (0..=lb as i32).collect();
    for i in 1..=la {
        let mut cur = vec![i as i32];
        for j in 1..=lb {
            let sub = prev[j - 1] + if aa[i - 1] == ba[j - 1] { 0 } else { 1 };
            let del = prev[j] + 1;
            let ins = cur[j - 1] + 1;
            cur.push(del.min(ins).min(sub));
        }
        prev = cur;
    }
    println!("{}", prev[lb]);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
)

func main() {
\ta := ""
\tif len(os.Args) > 1 { a = os.Args[1] }
\tb := ""
\tif len(os.Args) > 2 { b = os.Args[2] }
\trun := []rune(a)
\tcol := []rune(b)
\tla, lb := len(run), len(col)
\tprev := make([]int64, lb+1)
\tfor j := 0; j <= lb; j++ { prev[j] = int64(j) }
\tfor i := 1; i <= la; i++ {
\t\tcur := make([]int64, lb+1)
\t\tcur[0] = int64(i)
\t\tfor j := 1; j <= lb; j++ {
\t\t\tsub := prev[j-1] + 1
\t\t\tif run[i-1] == col[j-1] { sub-- }
\t\t\tdel, ins := prev[j]+1, cur[j-1]+1
\t\t\tm := del
\t\t\tif ins < m { m = ins }
\t\t\tif sub < m { m = sub }
\t\t\tcur[j] = m
\t\t}
\t\tprev = cur
\t}
\tfmt.Println(prev[lb])
}
''',
    },
    "lcs-length": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(int argc, char **argv) {
    const char *a = argc > 1 ? argv[1] : "";
    const char *b = argc > 2 ? argv[2] : "";
    size_t la = strlen(a), lb = strlen(b);
    int *prev = calloc(lb + 1, sizeof(int));
    for (size_t i = 1; i <= la; i++) {
        int *cur = calloc(lb + 1, sizeof(int));
        for (size_t j = 1; j <= lb; j++) {
            if (a[i - 1] == b[j - 1]) cur[j] = prev[j - 1] + 1;
            else cur[j] = prev[j] > cur[j - 1] ? prev[j] : cur[j - 1];
        }
        free(prev);
        prev = cur;
    }
    printf("%d\\n", prev[lb]);
    free(prev);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
a = sys.argv[1] if len(sys.argv) > 1 else ""
b = sys.argv[2] if len(sys.argv) > 2 else ""
prev = [0] * (len(b) + 1)
for ca in a:
    cur = [0]
    for j, cb in enumerate(b, 1):
        cur.append(prev[j - 1] + 1 if ca == cb else max(prev[j], cur[j - 1]))
    prev = cur
print(prev[-1])
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let a: String = args.get(1).cloned().unwrap_or_default();
    let b: String = args.get(2).cloned().unwrap_or_default();
    let ba: Vec<char> = b.chars().collect();
    let lb = ba.len();
    let mut prev = vec![0i32; lb + 1];
    for ca in a.chars() {
        let mut cur = vec![0i32; lb + 1];
        for j in 1..=lb {
            cur[j] = if ca == ba[j - 1] { prev[j - 1] + 1 } else { prev[j].max(cur[j - 1]) };
        }
        prev = cur;
    }
    println!("{}", prev[lb]);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
)

func main() {
\ta := ""
\tif len(os.Args) > 1 { a = os.Args[1] }
\tb := ""
\tif len(os.Args) > 2 { b = os.Args[2] }
\trun := []rune(a)
\tcol := []rune(b)
\tlb := len(col)
\tprev := make([]int64, lb+1)
\tfor _, ca := range run {
\t\tcur := make([]int64, lb+1)
\t\tfor j := 0; j < lb; j++ {
\t\t\tif ca == col[j] { cur[j+1] = prev[j] + 1 } else if prev[j+1] > cur[j] { cur[j+1] = prev[j+1] } else { cur[j+1] = cur[j] }
\t\t}
\t\tprev = cur
\t}
\tfmt.Println(prev[lb])
}
''',
    },
    "lpal-len": {
        "c": '''\
#include <stdio.h>
#include <string.h>
static int is_pal(const char *s, size_t i, size_t j) {
    while (i < j) if (s[i++] != s[j--]) return 0;
    return 1;
}
int main(int argc, char **argv) {
    const char *s = argc > 1 ? argv[1] : "";
    size_t n = strlen(s), best = 0;
    for (size_t i = 0; i < n; i++) {
        if (n - i <= best) break;
        for (size_t j = i; j < n; j++) {
            if (j - i + 1 <= best) continue;
            if (is_pal(s, i, j)) best = j - i + 1;
        }
    }
    printf("%zu\\n", best);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
s = sys.argv[1] if len(sys.argv) > 1 else ""
best = 0
n = len(s)
for i in range(n):
    if n - i <= best:
        break
    for j in range(i, n):
        if j - i + 1 <= best:
            continue
        t = s[i:j + 1]
        if t == t[::-1]:
            best = j - i + 1
print(best)
''',
        "rust": '''\
fn is_pal(v: &[char], i: usize, j: usize) -> bool {
    let (mut a, mut b) = (i, j);
    while a < b {
        if v[a] != v[b] { return false; }
        a += 1;
        b -= 1;
    }
    true
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let s: String = args.get(1).cloned().unwrap_or_default();
    let v: Vec<char> = s.chars().collect();
    let (n, mut best) = (v.len(), 0usize);
    for i in 0..n {
        if n - i <= best { break; }
        for j in i..n {
            if j - i + 1 <= best { continue; }
            if is_pal(&v, i, j) { best = j - i + 1; }
        }
    }
    println!("{}", best);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
)

func isPal(v []rune, i, j int) bool {
\tfor i < j {
\t\tif v[i] != v[j] { return false }
\t\ti++
\t\tj--
\t}
\treturn true
}

func main() {
\ts := ""
\tif len(os.Args) > 1 { s = os.Args[1] }
\tv := []rune(s)
\tn, best := len(v), 0
\tfor i := 0; i < n; i++ {
\t\tif n-i <= best { break }
\t\tfor j := i; j < n; j++ {
\t\t\tif j-i+1 <= best { continue }
\t\t\tif isPal(v, i, j) { best = j - i + 1 }
\t\t}
\t}
\tfmt.Println(best)
}
''',
    },
    "kmp-prefix": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(int argc, char **argv) {
    const char *s = argc > 1 ? argv[1] : "";
    size_t n = strlen(s);
    int *pi = malloc(n ? n * sizeof(int) : 1);
    if (n) pi[0] = 0;
    for (size_t i = 1; i < n; i++) {
        int j = pi[i - 1];
        while (j > 0 && s[i] != s[j]) j = pi[j - 1];
        if (s[i] == s[j]) j++;
        pi[i] = j;
    }
    for (size_t i = 0; i < n; i++)
        printf("%s%d", i ? " " : "", pi[i]);
    printf("\\n");
    free(pi);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
s = sys.argv[1] if len(sys.argv) > 1 else ""
pi = [0] * len(s)
for i in range(1, len(s)):
    j = pi[i - 1]
    while j > 0 and s[i] != s[j]:
        j = pi[j - 1]
    if s[i] == s[j]:
        j += 1
    pi[i] = j
print(" ".join(map(str, pi)))
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let s: String = args.get(1).cloned().unwrap_or_default();
    let v: Vec<char> = s.chars().collect();
    let mut pi: Vec<i32> = if v.is_empty() { Vec::new() } else { vec![0] };
    for i in 1..v.len() {
        let mut j = pi[i - 1] as usize;
        while j > 0 && v[i] != v[j] { j = pi[j - 1] as usize; }
        if v[i] == v[j] { j += 1; }
        pi.push(j as i32);
    }
    println!("{}", pi.iter().map(|x| x.to_string()).collect::<Vec<_>>().join(" "));
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strings"
)

func main() {
\ts := ""
\tif len(os.Args) > 1 { s = os.Args[1] }
\tv := []rune(s)
\tpi := make([]int, 0, len(v))
\tfor i := range v {
\t\tj := 0
\t\tif i > 0 { j = pi[i-1] }
\t\tfor j > 0 && v[i] != v[j] { j = pi[j-1] }
\t\tif v[i] == v[j] { j++ }
\t\tpi = append(pi, j)
\t}
\tout := make([]string, len(pi))
\tfor i, x := range pi { out[i] = fmt.Sprint(x) }
\tfmt.Println(strings.Join(out, " "))
}
''',
    },
    "caesar-shift": {
        "c": '''\
#include <stdio.h>
int main(int argc, char **argv) {
    const char *s = argc > 1 ? argv[1] : "";
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c >= 'a' && c <= 'z') c = 'a' + (c - 'a' + 3) % 26;
        else if (c >= 'A' && c <= 'Z') c = 'A' + (c - 'A' + 3) % 26;
        putchar(c);
    }
    printf("\\n");
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
s = sys.argv[1] if len(sys.argv) > 1 else ""
out = []
for ch in s:
    if "a" <= ch <= "z":
        out.append(chr(ord("a") + (ord(ch) - ord("a") + 3) % 26))
    elif "A" <= ch <= "Z":
        out.append(chr(ord("A") + (ord(ch) - ord("A") + 3) % 26))
    else:
        out.append(ch)
print("".join(out))
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let s: String = args.get(1).cloned().unwrap_or_default();
    let out: String = s.chars().map(|c| {
        if c.is_ascii_lowercase() {
            ('a' as u8 + (c as u8 - b'a' + 3) % 26) as char
        } else if c.is_ascii_uppercase() {
            ('A' as u8 + (c as u8 - b'A' + 3) % 26) as char
        } else {
            c
        }
    }).collect();
    println!("{}", out);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
)

func main() {
\ts := ""
\tif len(os.Args) > 1 { s = os.Args[1] }
\tb := []byte(s)
\tfor i, c := range b {
\t\tif c >= 'a' && c <= 'z' {
\t\t\tb[i] = 'a' + (c-'a'+3)%26
\t\t} else if c >= 'A' && c <= 'Z' {
\t\t\tb[i] = 'A' + (c-'A'+3)%26
\t\t}
\t}
\tfmt.Println(string(b))
}
''',
    },
    "happy-steps": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    long n = argc > 1 ? atol(argv[1]) : 0;
    long seen[64];
    int ns = 0, steps = 0;
    while (n != 1) {
        int dup = 0;
        for (int i = 0; i < ns; i++) if (seen[i] == n) dup = 1;
        if (dup) break;
        seen[ns++] = n;
        long s = 0, m = n;
        while (m) { s += (m % 10) * (m % 10); m /= 10; }
        n = s;
        steps++;
    }
    printf("%d\\n", steps);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
seen, s = set(), 0
while n != 1:
    if n in seen:
        break
    seen.add(n)
    n = sum(int(c) ** 2 for c in str(n))
    s += 1
print(s)
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut n: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let mut seen: Vec<u64> = Vec::new();
    let mut steps = 0u64;
    while n != 1 {
        if seen.contains(&n) { break; }
        seen.push(n);
        let s: u64 = n.to_string().chars().map(|c| (c as u8 - b'0').pow(2) as u64).sum();
        n = s;
        steps += 1;
    }
    println!("{}", steps);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

func main() {
\tn := int64(0)
\tif len(os.Args) > 1 { n, _ = strconv.ParseInt(os.Args[1], 10, 64) }
\tseen := map[int64]bool{}
\tsteps := int64(0)
\tfor n != 1 {
\t\tif seen[n] { break }
\t\tseen[n] = true
\t\ts := int64(0)
\t\tfor _, c := range strconv.FormatInt(n, 10) { s += int64(c-'0') * int64(c-'0') }
\t\tn = s
\t\tsteps++
\t}
\tfmt.Println(steps)
}
''',
    },
    "modexp": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    unsigned long a = argc > 1 ? strtoul(argv[1], 0, 10) : 0;
    unsigned long b = argc > 2 ? strtoul(argv[2], 0, 10) : 0;
    unsigned long m = argc > 3 ? strtoul(argv[3], 0, 10) : 1;
    if (m == 1) { printf("0\\n"); return 0; }
    unsigned long r = 1 % m;
    a %= m;
    while (b) {
        if (b & 1) r = (r * a) % m;
        a = (a * a) % m;
        b >>= 1;
    }
    printf("%lu\\n", r);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
a = int(sys.argv[1]) if len(sys.argv) > 1 else 0
b = int(sys.argv[2]) if len(sys.argv) > 2 else 0
m = int(sys.argv[3]) if len(sys.argv) > 3 else 1
print(pow(a, b, m))
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let a: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let b: u64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(0);
    let m: u64 = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(1);
    if m == 1 { println!("0"); return; }
    let (mut r, mut a) = (1 % m, a % m);
    let mut b = b;
    while b > 0 {
        if b & 1 == 1 { r = (r * a) % m; }
        a = (a * a) % m;
        b >>= 1;
    }
    println!("{}", r);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

func main() {
\ta, b := int64(0), int64(0)
\tm := int64(1)
\tif len(os.Args) > 1 { a, _ = strconv.ParseInt(os.Args[1], 10, 64) }
\tif len(os.Args) > 2 { b, _ = strconv.ParseInt(os.Args[2], 10, 64) }
\tif len(os.Args) > 3 { m, _ = strconv.ParseInt(os.Args[3], 10, 64) }
\tif m == 1 { fmt.Println(0); return }
\tr, a := int64(1)%m, a%m
\tfor b > 0 {
\t\tif b&1 == 1 { r = (r * a) % m }
\t\ta = (a * a) % m
\t\tb >>= 1
\t}
\tfmt.Println(r)
}
''',
    },
    "max-subarray": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
static long *parse_list(const char *s, int *out_n) {
    int cap = 64, n = 0;
    long *a = malloc(cap * sizeof(long));
    const char *p = s;
    while (*p) {
        while (*p == ' ') p++;
        if (!*p) break;
        if (n == cap) { cap *= 2; a = realloc(a, cap * sizeof(long)); }
        char *end;
        a[n++] = strtol(p, &end, 10);
        p = end;
    }
    *out_n = n;
    return a;
}
int main(int argc, char **argv) {
    int n;
    long *a = parse_list(argc > 1 ? argv[1] : "", &n);
    if (n == 0) { printf("0\\n"); free(a); return 0; }
    long best = a[0];
    for (int i = 0; i < n; i++) {
        long sum = 0;
        for (int j = i; j < n; j++) {
            sum += a[j];
            if (sum > best) best = sum;
        }
    }
    printf("%ld\\n", best);
    free(a);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
nums = [int(x) for x in (sys.argv[1].split() if len(sys.argv) > 1 else [])]
if not nums:
    print(0)
else:
    best = None
    for i in range(len(nums)):
        s = 0
        for j in range(i, len(nums)):
            s += nums[j]
            if best is None or s > best:
                best = s
    print(best)
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let s: String = args.get(1).cloned().unwrap_or_default();
    let nums: Vec<i64> = s.split_whitespace().filter_map(|x| x.parse().ok()).collect();
    if nums.is_empty() { println!("0"); return; }
    let n = nums.len();
    let mut best = nums[0];
    for i in 0..n {
        let mut sum = 0i64;
        for j in i..n {
            sum += nums[j];
            if sum > best { best = sum; }
        }
    }
    println!("{}", best);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
\t"strings"
)

func main() {
\ts := ""
\tif len(os.Args) > 1 { s = os.Args[1] }
\tnums := []int64{}
\tfor _, x := range strings.Fields(s) {
\t\tif v, err := strconv.ParseInt(x, 10, 64); err == nil { nums = append(nums, v) }
\t}
\tif len(nums) == 0 { fmt.Println(0); return }
\tn := len(nums)
\tbest := nums[0]
\tfor i := 0; i < n; i++ {
\t\tvar sum int64
\t\tfor j := i; j < n; j++ {
\t\t\tsum += nums[j]
\t\t\tif sum > best { best = sum }
\t\t}
\t}
\tfmt.Println(best)
}
''',
    },
    "count-inversions": {
        "c": '''\
#include <stdio.h>
#include <stdlib.h>
static long *parse_list(const char *s, int *out_n) {
    int cap = 64, n = 0;
    long *a = malloc(cap * sizeof(long));
    const char *p = s;
    while (*p) {
        while (*p == ' ') p++;
        if (!*p) break;
        if (n == cap) { cap *= 2; a = realloc(a, cap * sizeof(long)); }
        char *end;
        a[n++] = strtol(p, &end, 10);
        p = end;
    }
    *out_n = n;
    return a;
}
int main(int argc, char **argv) {
    int n;
    long *a = parse_list(argc > 1 ? argv[1] : "", &n);
    long c = 0;
    for (int i = 0; i < n; i++)
        for (int j = i + 1; j < n; j++)
            if (a[i] > a[j]) c++;
    printf("%ld\\n", c);
    free(a);
    return 0;
}
''',
        "python": '''\
#!/usr/bin/env python3
import sys
nums = [int(x) for x in (sys.argv[1].split() if len(sys.argv) > 1 else [])]
c = 0
for i in range(len(nums)):
    for j in range(i + 1, len(nums)):
        if nums[i] > nums[j]:
            c += 1
print(c)
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let s: String = args.get(1).cloned().unwrap_or_default();
    let nums: Vec<i64> = s.split_whitespace().filter_map(|x| x.parse().ok()).collect();
    let n = nums.len();
    let mut c = 0i64;
    for i in 0..n {
        for j in (i + 1)..n {
            if nums[i] > nums[j] { c += 1; }
        }
    }
    println!("{}", c);
}
''',
        "go": '''\
package main

import (
\t"fmt"
\t"os"
\t"strconv"
\t"strings"
)

func main() {
\ts := ""
\tif len(os.Args) > 1 { s = os.Args[1] }
\tnums := []int64{}
\tfor _, x := range strings.Fields(s) {
\t\tif v, err := strconv.ParseInt(x, 10, 64); err == nil { nums = append(nums, v) }
\t}
\tn := len(nums)
\tc := int64(0)
\tfor i := 0; i < n; i++ {
\t\tfor j := i + 1; j < n; j++ {
\t\t\tif nums[i] > nums[j] { c++ }
\t\t}
\t}
\tfmt.Println(c)
}
''',
    },
}

# --------------------------------------------------------------------------- #
# language porting layer — baselines for languages whose source is a
# mechanical transform of an existing one. Derived at import time, verified
# by the same self-check sweep as hand-written ones (any transform bug fails
# the gate and never ships).
# --------------------------------------------------------------------------- #

def _c_to_cpp(src: str) -> str:
    """C -> C++: malloc/calloc/realloc return void* in C but need explicit
    casts in C++ (and nvcc's C++ frontend). Cast to the variable's declared
    type, tracked from `type *var` declarations anywhere in the source."""
    types = {m.group(2): m.group(1)
             for m in re.finditer(r"\b(\w+)\s*\*\s*(\w+)\s*[=;]", src)}

    def cast(m: "re.Match") -> str:
        var, fn = m.group(1), m.group(2)
        t = types.get(var)
        return f"{var} = ({t} *){fn}(" if t else m.group(0)

    return re.sub(r"\b(\w+)\s*=\s*(malloc|calloc|realloc)\(", cast, src)


for _k in list(_BASELINES):
    # CUDA compiles C host code as-is (nvcc accepts it; score runs the
    # artifact on CPU, so a pure-C baseline is a valid starting point).
    # nvcc uses a C++ frontend, so apply the same void* cast transform.
    _cpp_src = _c_to_cpp(_BASELINES[_k]["c"])
    _BASELINES[_k]["cuda"] = _cpp_src
    _BASELINES[_k]["cpp"] = _cpp_src
del _k

# --------------------------------------------------------------------------- #
# hand-written baselines: javascript, perl, shell — verifiable on this
# machine (node/perl/bash present), so the self-check sweep proves them.
# I/O contract is identical to the C baseline: argv in, one line out.
# --------------------------------------------------------------------------- #

_BASELINES["prime-count"]["javascript"] = '''\
const n = parseInt(process.argv[2] || "0", 10);
if (n < 2) { console.log(0); process.exit(0); }
const sieve = new Uint8Array(n);
for (let i = 2; i * i < n; i++) if (!sieve[i])
    for (let j = i * i; j < n; j += i) sieve[j] = 1;
let c = 0;
for (let i = 2; i < n; i++) if (!sieve[i]) c++;
console.log(c);
'''
_BASELINES["prime-count"]["perl"] = '''\
use strict; use warnings;
my $n = defined $ARGV[0] ? int($ARGV[0]) : 0;
print "0\\n" and exit if $n < 2;
my @sieve;
for (my $i = 2; $i * $i < $n; $i++) {
    next if $sieve[$i];
    for (my $j = $i * $i; $j < $n; $j += $i) { $sieve[$j] = 1; }
}
my $c = 0;
for my $i (2 .. $n - 1) { $c++ unless $sieve[$i]; }
print "$c\\n";
'''
_BASELINES["prime-count"]["shell"] = '''\
n=${1:-0}
if [ "$n" -lt 2 ]; then echo 0; exit 0; fi
sieve=()
for (( i = 2; i * i < n; i++ )); do
    if [ "${sieve[$i]:-0}" -eq 0 ]; then
        for (( j = i * i; j < n; j += i )); do sieve[$j]=1; done
    fi
done
c=0
for (( i = 2; i < n; i++ )); do
    if [ "${sieve[$i]:-0}" -eq 0 ]; then c=$((c + 1)); fi
done
echo "$c"
'''

_BASELINES["popcount"]["javascript"] = '''\
let x = parseInt(process.argv[2] || "0", 10);
let c = 0;
while (x) { c += x & 1; x >>>= 1; }
console.log(c);
'''
_BASELINES["popcount"]["perl"] = '''\
my $x = defined $ARGV[0] ? int($ARGV[0]) : 0;
my $c = 0;
while ($x) { $c += $x & 1; $x >>= 1; }
print "$c\\n";
'''
_BASELINES["popcount"]["shell"] = '''\
x=${1:-0}
c=0
while [ "$x" -gt 0 ]; do
    c=$((c + (x & 1)))
    x=$((x >> 1))
done
echo "$c"
'''

_BASELINES["gcd"]["javascript"] = '''\
let a = parseInt(process.argv[2] || "0", 10);
let b = parseInt(process.argv[3] || "0", 10);
while (b) { const t = a % b; a = b; b = t; }
console.log(a);
'''
_BASELINES["gcd"]["perl"] = '''\
my $a = defined $ARGV[0] ? int($ARGV[0]) : 0;
my $b = defined $ARGV[1] ? int($ARGV[1]) : 0;
while ($b) { my $t = $a % $b; $a = $b; $b = $t; }
print "$a\\n";
'''
_BASELINES["gcd"]["shell"] = '''\
a=${1:-0}
b=${2:-0}
while [ "$b" -ne 0 ]; do
    t=$((a % b))
    a=$b
    b=$t
done
echo "$a"
'''

_BASELINES["fib-mod"]["javascript"] = '''\
const MOD = 1000000007n;
let n = BigInt(process.argv[2] || "0");
let a = 0n, b = 1n;
for (let i = 0n; i < n; i++) { const t = a + b; a = b; b = t % MOD; }
console.log(a.toString());
'''
_BASELINES["fib-mod"]["perl"] = '''\
my $MOD = 1000000007;
my $n = defined $ARGV[0] ? int($ARGV[0]) : 0;
my ($a, $b) = (0, 1);
for my $i (1 .. $n) { my $t = $a + $b; $a = $b; $b = $t % $MOD; }
print "$a\\n";
'''
_BASELINES["fib-mod"]["shell"] = '''\
MOD=1000000007
n=${1:-0}
a=0
b=1
for (( i = 0; i < n; i++ )); do
    t=$((a + b))
    a=$b
    b=$((t % MOD))
done
echo "$a"
'''

_BASELINES["num-divisors"]["javascript"] = '''\
const n = parseInt(process.argv[2] || "0", 10);
let c = 0;
for (let d = 1; d * d <= n; d++)
    if (n % d === 0) c += (d * d === n) ? 1 : 2;
console.log(c);
'''
_BASELINES["num-divisors"]["perl"] = '''\
my $n = defined $ARGV[0] ? int($ARGV[0]) : 0;
my $c = 0;
for (my $d = 1; $d * $d <= $n; $d++) {
    if ($n % $d == 0) { $c += ($d * $d == $n) ? 1 : 2; }
}
print "$c\\n";
'''
_BASELINES["num-divisors"]["shell"] = '''\
n=${1:-0}
c=0
for (( d = 1; d * d <= n; d++ )); do
    if (( n % d == 0 )); then
        if (( d * d == n )); then c=$((c + 1)); else c=$((c + 2)); fi
    fi
done
echo "$c"
'''

_BASELINES["collatz-steps"]["javascript"] = '''\
let n = parseInt(process.argv[2] || "0", 10);
let s = 0;
while (n > 1) { if (n % 2 === 0) n /= 2; else n = 3 * n + 1; s++; }
console.log(s);
'''
_BASELINES["collatz-steps"]["perl"] = '''\
my $n = defined $ARGV[0] ? int($ARGV[0]) : 0;
my $s = 0;
while ($n > 1) { if ($n % 2 == 0) { $n /= 2; } else { $n = 3 * $n + 1; } $s++; }
print "$s\\n";
'''
_BASELINES["collatz-steps"]["shell"] = '''\
n=${1:-0}
s=0
while [ "$n" -gt 1 ]; do
    if (( n % 2 == 0 )); then n=$((n / 2)); else n=$((3 * n + 1)); fi
    s=$((s + 1))
done
echo "$s"
'''

_BASELINES["sum-range"]["javascript"] = '''\
const MOD = 1000000007;
const n = parseInt(process.argv[2] || "0", 10);
let s = 0;
for (let i = 1; i <= n; i++) s = (s + i) % MOD;
console.log(s);
'''
_BASELINES["sum-range"]["perl"] = '''\
my $MOD = 1000000007;
my $n = defined $ARGV[0] ? int($ARGV[0]) : 0;
my $s = 0;
for my $i (1 .. $n) { $s = ($s + $i) % $MOD; }
print "$s\\n";
'''
_BASELINES["sum-range"]["shell"] = '''\
MOD=1000000007
n=${1:-0}
s=0
for (( i = 1; i <= n; i++ )); do
    s=$(( (s + i) % MOD ))
done
echo "$s"
'''

_BASELINES["reverse-str"]["javascript"] = '''\
const s = process.argv[2] || "";
console.log([...s].reverse().join(""));
'''
_BASELINES["reverse-str"]["perl"] = '''\
my $s = defined $ARGV[0] ? $ARGV[0] : "";
my $r = scalar reverse $s;
print "$r\\n";
'''
_BASELINES["reverse-str"]["shell"] = '''\
s=${1:-}
out=""
for (( i = ${#s} - 1; i >= 0; i-- )); do
    out+=${s:i:1}
done
echo "$out"
'''

_BASELINES["is-palindrome"]["javascript"] = '''\
const s = process.argv[2] || "";
console.log(s === [...s].reverse().join("") ? 1 : 0);
'''
_BASELINES["is-palindrome"]["perl"] = '''\
my $s = defined $ARGV[0] ? $ARGV[0] : "";
print((reverse $s) eq $s ? "1\\n" : "0\\n");
'''
_BASELINES["is-palindrome"]["shell"] = '''\
s=${1:-}
ok=1
n=${#s}
for (( i = 0; i < n / 2; i++ )); do
    if [ "${s:i:1}" != "${s:n-i-1:1}" ]; then ok=0; break; fi
done
echo "$ok"
'''

_BASELINES["rle"]["javascript"] = '''\
const s = process.argv[2] || "";
let out = "";
for (let i = 0; i < s.length; ) {
    let j = i;
    while (j < s.length && s[j] === s[i]) j++;
    out += s[i] + (j - i);
    i = j;
}
console.log(out);
'''
_BASELINES["rle"]["perl"] = '''\
my $s = defined $ARGV[0] ? $ARGV[0] : "";
$s =~ s/(.)\\1*/$1 . length($&)/ge;
print "$s\\n";
'''
_BASELINES["rle"]["shell"] = '''\
s=${1:-}
out=""
i=0
n=${#s}
while [ "$i" -lt "$n" ]; do
    j=$i
    while [ "$j" -lt "$n" ] && [ "${s:j:1}" = "${s:i:1}" ]; do j=$((j + 1)); done
    out+=${s:i:1}$((j - i))
    i=$j
done
echo "$out"
'''

_BASELINES["is-prime"]["javascript"] = '''\
const n = parseInt(process.argv[2] || "0", 10);
if (n < 2) { console.log(0); process.exit(0); }
for (let d = 2; d * d <= n; d++)
    if (n % d === 0) { console.log(0); process.exit(0); }
console.log(1);
'''
_BASELINES["is-prime"]["perl"] = '''\
my $n = defined $ARGV[0] ? int($ARGV[0]) : 0;
print "0\\n" and exit if $n < 2;
for (my $d = 2; $d * $d <= $n; $d++) {
    print "0\\n" and exit if $n % $d == 0;
}
print "1\\n";
'''
_BASELINES["is-prime"]["shell"] = '''\
n=${1:-0}
if [ "$n" -lt 2 ]; then echo 0; exit 0; fi
for (( d = 2; d * d <= n; d++ )); do
    if (( n % d == 0 )); then echo 0; exit 0; fi
done
echo 1
'''

_BASELINES["int-sqrt"]["javascript"] = '''\
const n = parseInt(process.argv[2] || "0", 10);
let lo = 0, hi = n + 1;
while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (mid * mid <= n) lo = mid; else hi = mid;
}
console.log(lo);
'''
_BASELINES["int-sqrt"]["perl"] = '''\
my $n = defined $ARGV[0] ? int($ARGV[0]) : 0;
my ($lo, $hi) = (0, $n + 1);
while ($hi - $lo > 1) {
    my $mid = int(($lo + $hi) / 2);
    if ($mid * $mid <= $n) { $lo = $mid; } else { $hi = $mid; }
}
print "$lo\\n";
'''
_BASELINES["int-sqrt"]["shell"] = '''\
n=${1:-0}
lo=0
hi=$((n + 1))
while [ $((hi - lo)) -gt 1 ]; do
    mid=$(((lo + hi) / 2))
    if (( mid * mid <= n )); then lo=$mid; else hi=$mid; fi
done
echo "$lo"
'''

_BASELINES["digital-root"]["javascript"] = '''\
let n = parseInt(process.argv[2] || "0", 10);
while (n >= 10) {
    let s = 0;
    while (n) { s += n % 10; n = Math.floor(n / 10); }
    n = s;
}
console.log(n);
'''
_BASELINES["digital-root"]["perl"] = '''\
my $n = defined $ARGV[0] ? int($ARGV[0]) : 0;
while ($n >= 10) {
    my $s = 0;
    while ($n) { $s += $n % 10; $n = int($n / 10); }
    $n = $s;
}
print "$n\\n";
'''
_BASELINES["digital-root"]["shell"] = '''\
n=${1:-0}
while [ "$n" -ge 10 ]; do
    s=0
    m=$n
    while [ "$m" -gt 0 ]; do
        s=$((s + m % 10))
        m=$((m / 10))
    done
    n=$s
done
echo "$n"
'''

_BASELINES["trailing-zeros"]["javascript"] = '''\
let n = parseInt(process.argv[2] || "0", 10);
let c = 0;
while (n !== 0 && n % 2 === 0) { c++; n /= 2; }
console.log(c);
'''
_BASELINES["trailing-zeros"]["perl"] = '''\
my $n = defined $ARGV[0] ? int($ARGV[0]) : 0;
my $c = 0;
while ($n != 0 && $n % 2 == 0) { $c++; $n /= 2; }
print "$c\\n";
'''
_BASELINES["trailing-zeros"]["shell"] = '''\
n=${1:-0}
c=0
while [ "$n" -ne 0 ] && [ $((n % 2)) -eq 0 ]; do
    c=$((c + 1))
    n=$((n / 2))
done
echo "$c"
'''

_BASELINES["omega"]["javascript"] = '''\
let n = parseInt(process.argv[2] || "1", 10);
let c = 0, d = 2;
while (d * d <= n) {
    while (n % d === 0) { c++; n /= d; }
    d++;
}
if (n > 1) c++;
console.log(c);
'''
_BASELINES["omega"]["perl"] = '''\
my $n = defined $ARGV[0] ? int($ARGV[0]) : 1;
my ($c, $d) = (0, 2);
while ($d * $d <= $n) {
    while ($n % $d == 0) { $c++; $n /= $d; }
    $d++;
}
$c++ if $n > 1;
print "$c\\n";
'''
_BASELINES["omega"]["shell"] = '''\
n=${1:-1}
c=0
d=2
while (( d * d <= n )); do
    while (( n % d == 0 )); do c=$((c + 1)); n=$((n / d)); done
    d=$((d + 1))
done
if [ "$n" -gt 1 ]; then c=$((c + 1)); fi
echo "$c"
'''

_BASELINES["nth-prime"]["javascript"] = '''\
function isP(x) {
    if (x < 2) return false;
    for (let d = 2; d * d <= x; d++) if (x % d === 0) return false;
    return true;
}
const k = parseInt(process.argv[2] || "0", 10);
let c = 0, x = 1;
while (c < k) { x++; if (isP(x)) c++; }
console.log(x);
'''
_BASELINES["nth-prime"]["perl"] = '''\
sub is_p { my $x = shift; return 0 if $x < 2;
    for (my $d = 2; $d * $d <= $x; $d++) { return 0 if $x % $d == 0; }
    return 1; }
my $k = defined $ARGV[0] ? int($ARGV[0]) : 0;
my ($c, $x) = (0, 1);
while ($c < $k) { $x++; $c++ if is_p($x); }
print "$x\\n";
'''
_BASELINES["nth-prime"]["shell"] = '''\
k=${1:-0}
c=0
x=1
while [ "$c" -lt "$k" ]; do
    x=$((x + 1))
    is_p=1
    for (( d = 2; d * d <= x; d++ )); do
        if (( x % d == 0 )); then is_p=0; break; fi
    done
    if [ "$is_p" -eq 1 ]; then c=$((c + 1)); fi
done
echo "$x"
'''

_BASELINES["levenshtein"]["javascript"] = '''\
const a = process.argv[2] || "";
const b = process.argv[3] || "";
let prev = Array.from({ length: b.length + 1 }, (_, j) => j);
for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    for (let j = 1; j <= b.length; j++) {
        const sub = prev[j - 1] + (a[i - 1] !== b[j - 1] ? 1 : 0);
        const del = prev[j] + 1, ins = cur[j - 1] + 1;
        cur[j] = Math.min(del, ins, sub);
    }
    prev = cur;
}
console.log(prev[b.length]);
'''
_BASELINES["levenshtein"]["perl"] = '''\
my $sa = defined $ARGV[0] ? $ARGV[0] : "";
my $sb = defined $ARGV[1] ? $ARGV[1] : "";
my @prev = (0 .. length $sb);
for my $i (1 .. length $sa) {
    my @cur = ($i);
    for my $j (1 .. length $sb) {
        my $sub = $prev[$j - 1] + (substr($sa, $i - 1, 1) ne substr($sb, $j - 1, 1));
        my $del = $prev[$j] + 1;
        my $ins = $cur[$j - 1] + 1;
        my $m = $del < $ins ? $del : $ins;
        $cur[$j] = $sub < $m ? $sub : $m;
    }
    @prev = @cur;
}
print "$prev[$#prev]\\n";
'''
_BASELINES["levenshtein"]["shell"] = '''\
a=${1:-}
b=${2:-}
la=${#a}
lb=${#b}
prev=()
for (( j = 0; j <= lb; j++ )); do prev[$j]=$j; done
for (( i = 1; i <= la; i++ )); do
    cur=()
    cur[0]=$i
    for (( j = 1; j <= lb; j++ )); do
        if [ "${a:i-1:1}" = "${b:j-1:1}" ]; then cost=0; else cost=1; fi
        sub=$((prev[j-1] + cost))
        del=$((prev[j] + 1))
        ins=$((cur[j-1] + 1))
        m=$sub
        [ "$del" -lt "$m" ] && m=$del
        [ "$ins" -lt "$m" ] && m=$ins
        cur[$j]=$m
    done
    prev=("${cur[@]}")
done
echo "${prev[$lb]}"
'''

_BASELINES["lcs-length"]["javascript"] = '''\
const a = process.argv[2] || "";
const b = process.argv[3] || "";
let prev = new Array(b.length + 1).fill(0);
for (let i = 1; i <= a.length; i++) {
    const cur = [0];
    for (let j = 1; j <= b.length; j++) {
        if (a[i - 1] === b[j - 1]) cur[j] = prev[j - 1] + 1;
        else cur[j] = Math.max(prev[j], cur[j - 1]);
    }
    prev = cur;
}
console.log(prev[b.length]);
'''
_BASELINES["lcs-length"]["perl"] = '''\
my $a = defined $ARGV[0] ? $ARGV[0] : "";
my $b = defined $ARGV[1] ? $ARGV[1] : "";
my @prev = (0) x (length($b) + 1);
for my $i (1 .. length $a) {
    my @cur = (0);
    for my $j (1 .. length $b) {
        if (substr($a, $i - 1, 1) eq substr($b, $j - 1, 1)) {
            $cur[$j] = $prev[$j - 1] + 1;
        } else {
            $cur[$j] = $prev[$j] > $cur[$j - 1] ? $prev[$j] : $cur[$j - 1];
        }
    }
    @prev = @cur;
}
my $last = $#prev;
print "$prev[$last]\\n";
'''
_BASELINES["lcs-length"]["shell"] = '''\
a=${1:-}
b=${2:-}
la=${#a}
lb=${#b}
prev=()
for (( j = 0; j <= lb; j++ )); do prev[$j]=0; done
for (( i = 1; i <= la; i++ )); do
    cur=()
    cur[0]=0
    for (( j = 1; j <= lb; j++ )); do
        if [ "${a:i-1:1}" = "${b:j-1:1}" ]; then
            cur[$j]=$((prev[j-1] + 1))
        elif [ "${prev[j]}" -gt "${cur[j-1]}" ]; then
            cur[$j]=${prev[j]}
        else
            cur[$j]=${cur[j-1]}
        fi
    done
    prev=("${cur[@]}")
done
echo "${prev[$lb]}"
'''

_BASELINES["lpal-len"]["javascript"] = '''\
const s = process.argv[2] || "";
function isPal(i, j) {
    while (i < j) if (s[i++] !== s[j--]) return false;
    return true;
}
let best = 0;
for (let i = 0; i < s.length; i++) {
    if (s.length - i <= best) break;
    for (let j = i; j < s.length; j++) {
        if (j - i + 1 > best && isPal(i, j)) best = j - i + 1;
    }
}
console.log(best);
'''
_BASELINES["lpal-len"]["perl"] = '''\
my $s = defined $ARGV[0] ? $ARGV[0] : "";
sub is_pal { my ($i, $j) = @_;
    while ($i < $j) { return 0 if substr($s, $i++, 1) ne substr($s, $j--, 1); }
    return 1; }
my $n = length $s;
my $best = 0;
for my $i (0 .. $n - 1) {
    last if $n - $i <= $best;
    for my $j ($i .. $n - 1) {
        next if $j - $i + 1 <= $best;
        $best = $j - $i + 1 if is_pal($i, $j);
    }
}
print "$best\\n";
'''
_BASELINES["lpal-len"]["shell"] = '''\
s=${1:-}
n=${#s}
is_pal() {
    local i=$1 j=$2
    while [ "$i" -lt "$j" ]; do
        if [ "${s:i:1}" != "${s:j:1}" ]; then return 1; fi
        i=$((i + 1)); j=$((j - 1))
    done
    return 0
}
best=0
for (( i = 0; i < n; i++ )); do
    [ $((n - i)) -le "$best" ] && break
    for (( j = i; j < n; j++ )); do
        if [ $((j - i + 1)) -gt "$best" ] && is_pal "$i" "$j"; then
            best=$((j - i + 1))
        fi
    done
done
echo "$best"
'''

_BASELINES["kmp-prefix"]["javascript"] = '''\
const s = process.argv[2] || "";
const pi = new Array(s.length).fill(0);
for (let i = 0; i < s.length; i++) {
    let j = i ? pi[i - 1] : 0;
    while (j > 0 && s[i] !== s[j]) j = pi[j - 1];
    if (s[i] === s[j]) j++;
    pi[i] = j;
}
console.log(pi.join(" "));
'''
_BASELINES["kmp-prefix"]["perl"] = '''\
my $s = defined $ARGV[0] ? $ARGV[0] : "";
my @pi = (0) x length($s);
for my $i (0 .. length($s) - 1) {
    my $j = $i ? $pi[$i - 1] : 0;
    while ($j > 0 && substr($s, $i, 1) ne substr($s, $j, 1)) { $j = $pi[$j - 1]; }
    $j++ if substr($s, $i, 1) eq substr($s, $j, 1);
    $pi[$i] = $j;
}
print join(" ", @pi), "\\n";
'''
_BASELINES["kmp-prefix"]["shell"] = '''\
s=${1:-}
n=${#s}
pi=()
for (( i = 0; i < n; i++ )); do
    if [ "$i" -gt 0 ]; then j=${pi[i-1]}; else j=0; fi
    while [ "$j" -gt 0 ] && [ "${s:i:1}" != "${s:j:1}" ]; do j=${pi[j-1]}; done
    if [ "${s:i:1}" = "${s:j:1}" ]; then j=$((j + 1)); fi
    pi[$i]=$j
done
echo "${pi[*]}"
'''

_BASELINES["caesar-shift"]["javascript"] = '''\
const s = process.argv[2] || "";
let out = "";
for (const ch of s) {
    const c = ch.charCodeAt(0);
    if (c >= 97 && c <= 122) out += String.fromCharCode(97 + (c - 97 + 3) % 26);
    else if (c >= 65 && c <= 90) out += String.fromCharCode(65 + (c - 65 + 3) % 26);
    else out += ch;
}
console.log(out);
'''
_BASELINES["caesar-shift"]["perl"] = '''\
my $s = defined $ARGV[0] ? $ARGV[0] : "";
my $out = "";
for my $ch (split //, $s) {
    my $c = ord $ch;
    if ($c >= 97 && $c <= 122) { $out .= chr(97 + ($c - 97 + 3) % 26); }
    elsif ($c >= 65 && $c <= 90) { $out .= chr(65 + ($c - 65 + 3) % 26); }
    else { $out .= $ch; }
}
print "$out\\n";
'''
_BASELINES["caesar-shift"]["shell"] = '''\
s=${1:-}
out=""
for (( i = 0; i < ${#s}; i++ )); do
    ch=${s:i:1}
    printf -v c '%d' "'$ch"
    if [ "$c" -ge 97 ] && [ "$c" -le 122 ]; then
        out+=$(printf "\\\\$(printf %03o $((97 + (c - 97 + 3) % 26)))")
    elif [ "$c" -ge 65 ] && [ "$c" -le 90 ]; then
        out+=$(printf "\\\\$(printf %03o $((65 + (c - 65 + 3) % 26)))")
    else
        out+=$ch
    fi
done
echo "$out"
'''

_BASELINES["happy-steps"]["javascript"] = '''\
let n = parseInt(process.argv[2] || "0", 10);
const seen = new Set();
let steps = 0;
while (n !== 1) {
    if (seen.has(n)) break;
    seen.add(n);
    let s = 0, m = n;
    while (m) { s += (m % 10) * (m % 10); m = Math.floor(m / 10); }
    n = s;
    steps++;
}
console.log(steps);
'''
_BASELINES["happy-steps"]["perl"] = '''\
my $n = defined $ARGV[0] ? int($ARGV[0]) : 0;
my %seen;
my $steps = 0;
while ($n != 1) {
    last if $seen{$n}++;
    my $s = 0;
    for my $d (split //, "$n") { $s += $d * $d; }
    $n = $s;
    $steps++;
}
print "$steps\\n";
'''
_BASELINES["happy-steps"]["shell"] = '''\
n=${1:-0}
declare -A seen=()
steps=0
while [ "$n" -ne 1 ]; do
    if [ -n "${seen[$n]:-}" ]; then break; fi
    seen[$n]=1
    s=0
    m=$n
    while [ "$m" -gt 0 ]; do
        d=$((m % 10))
        s=$((s + d * d))
        m=$((m / 10))
    done
    n=$s
    steps=$((steps + 1))
done
echo "$steps"
'''

_BASELINES["modexp"]["javascript"] = '''\
let a = BigInt(process.argv[2] || "0");
let b = BigInt(process.argv[3] || "0");
const m = BigInt(process.argv[4] || "1");
if (m === 1n) { console.log(0); process.exit(0); }
let r = 1n % m;
a %= m;
while (b > 0n) {
    if (b & 1n) r = (r * a) % m;
    a = (a * a) % m;
    b >>= 1n;
}
console.log(r.toString());
'''
_BASELINES["modexp"]["perl"] = '''\
my $a = defined $ARGV[0] ? int($ARGV[0]) : 0;
my $b = defined $ARGV[1] ? int($ARGV[1]) : 0;
my $m = defined $ARGV[2] ? int($ARGV[2]) : 1;
print "0\\n" and exit if $m == 1;
my $r = 1 % $m;
$a %= $m;
while ($b) {
    $r = ($r * $a) % $m if $b & 1;
    $a = ($a * $a) % $m;
    $b >>= 1;
}
print "$r\\n";
'''
_BASELINES["modexp"]["shell"] = '''\
a=${1:-0}
b=${2:-0}
m=${3:-1}
if [ "$m" -eq 1 ]; then echo 0; exit 0; fi
r=$((1 % m))
a=$((a % m))
while [ "$b" -ne 0 ]; do
    if (( b & 1 )); then r=$(( (r * a) % m )); fi
    a=$(( (a * a) % m ))
    b=$((b >> 1))
done
echo "$r"
'''

_BASELINES["max-subarray"]["javascript"] = '''\
const nums = (process.argv[2] || "").split(" ").filter(Boolean).map(Number);
if (!nums.length) { console.log(0); process.exit(0); }
let best = nums[0];
for (let i = 0; i < nums.length; i++) {
    let sum = 0;
    for (let j = i; j < nums.length; j++) {
        sum += nums[j];
        if (sum > best) best = sum;
    }
}
console.log(best);
'''
_BASELINES["max-subarray"]["perl"] = '''\
my @nums = split /\\s+/, (defined $ARGV[0] ? $ARGV[0] : "");
@nums = grep { length } @nums;
print "0\\n" and exit unless @nums;
my $best = $nums[0];
for my $i (0 .. $#nums) {
    my $sum = 0;
    for my $j ($i .. $#nums) {
        $sum += $nums[$j];
        $best = $sum if $sum > $best;
    }
}
print "$best\\n";
'''
_BASELINES["max-subarray"]["shell"] = '''\
read -ra nums <<< "${1:-}"
if [ ${#nums[@]} -eq 0 ]; then echo 0; exit 0; fi
best=${nums[0]}
for (( i = 0; i < ${#nums[@]}; i++ )); do
    sum=0
    for (( j = i; j < ${#nums[@]}; j++ )); do
        sum=$((sum + nums[j]))
        if [ "$sum" -gt "$best" ]; then best=$sum; fi
    done
done
echo "$best"
'''

_BASELINES["count-inversions"]["javascript"] = '''\
const nums = (process.argv[2] || "").split(" ").filter(Boolean).map(Number);
let c = 0;
for (let i = 0; i < nums.length; i++)
    for (let j = i + 1; j < nums.length; j++)
        if (nums[i] > nums[j]) c++;
console.log(c);
'''
_BASELINES["count-inversions"]["perl"] = '''\
my @nums = split /\\s+/, (defined $ARGV[0] ? $ARGV[0] : "");
@nums = grep { length } @nums;
my $c = 0;
for my $i (0 .. $#nums) {
    for my $j ($i + 1 .. $#nums) {
        $c++ if $nums[$i] > $nums[$j];
    }
}
print "$c\\n";
'''
_BASELINES["count-inversions"]["shell"] = '''\
read -ra nums <<< "${1:-}"
c=0
for (( i = 0; i < ${#nums[@]}; i++ )); do
    for (( j = i + 1; j < ${#nums[@]}; j++ )); do
        if [ "${nums[i]}" -gt "${nums[j]}" ]; then c=$((c + 1)); fi
    done
done
echo "$c"
'''

# --------------------------------------------------------------------------- #
# algorithm registry
# --------------------------------------------------------------------------- #

ALGORITHMS: "Dict[str, Dict[str, Any]]" = {}


def _algo(key: str, name: str, family: str, domain: Dict[str, Any], goal: str,
          workload: Optional[Dict[str, List[List[str]]]] = None, compare: str = "exact",
          ascii_only: bool = False,
          domains: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    ALGORITHMS[key] = {
        "key": key,
        "name": name,
        "family": family,
        "domain": domain,
        "domains": domains or {},
        "compare": compare,
        "ascii_only": ascii_only,
        "goal": goal,
        "workload": workload,
        "ref": _REF[key],
        "baselines": _BASELINES[key],
    }


_algo("prime-count", "Prime Counter", "int", {"lo": 0, "hi": 10 ** 6},
      "Count the primes below n as fast as possible while keeping the exact "
      "output contract: print one integer. Prefer algorithmic wins (sieve, "
      "odd-only, wheel factorization) over micro-tuning.",
      {"c": [["100000"]] * 30, "rust": [["100000"]] * 30, "go": [["100000"]] * 30,
       "python": [["40000"]] * 8},
      domains={"python": {"hi": 20000}})

_algo("popcount", "Popcount (set-bit counter)", "int", {"lo": 0, "hi": 2 ** 31 - 1},
      "Count the set bits of n as fast as possible: print one integer. "
      "Look for bit-twiddling wins (parallel prefix counts, lookup tables) "
      "while keeping the exact output.",
      {l: _int_stream(7, 400, 0, 2 ** 31 - 1) for l in LANG_EXT})

_algo("gcd", "GCD of two integers", "pair_int",
      {"alo": 1, "ahi": 2 ** 31 - 1, "blo": 1, "bhi": 2 ** 31 - 1},
      "Compute gcd(a, b) as fast as possible: print one integer. Binary GCD "
      "and instruction-level tricks are fair game; the output must stay exact.",
      {l: _pair_stream(7, 400, 1, 2 ** 31 - 1, 1, 2 ** 31 - 1) for l in LANG_EXT})

_algo("fib-mod", "Fibonacci(n) mod 1e9+7", "int", {"lo": 0, "hi": 10 ** 6},
      "Compute fib(n) mod 1000000007 as fast as possible: print one integer. "
      "The naive loop is O(n); fast doubling is O(log n). Output must stay exact.",
      {"c": ["1000000"] * 3 + ["2000000", "3000000"],
       "rust": ["1000000"] * 3 + ["2000000", "3000000"],
       "go": ["1000000"] * 3 + ["2000000", "3000000"],
       "python": ["200000"] * 3})

_algo("num-divisors", "Number of divisors", "int", {"lo": 1, "hi": 10 ** 6},
      "Count the positive divisors of n as fast as possible: print one integer. "
      "Prime factorization beats trial division; output must stay exact.",
      {l: _int_stream(7, 300, 1, 10 ** 6) for l in LANG_EXT})

_algo("collatz-steps", "Collatz stopping time", "int", {"lo": 1, "hi": 10 ** 7},
      "Count the Collatz steps to reach 1 (n -> n/2 if even, 3n+1 if odd; "
      "n = 1 takes 0 steps): print one integer. Use unsigned 64-bit "
      "arithmetic — trajectories can exceed 2^63; output must stay exact.",
      {l: _int_stream(7, 400, 1, 10 ** 7) for l in LANG_EXT})

_algo("sum-range", "Sum 1..n mod 1e9+7", "int", {"lo": 0, "hi": 10 ** 6},
      "Print the sum of 1..n modulo 1000000007. The naive loop is O(n); the "
      "closed form n(n+1)/2 is O(1) — but watch integer overflow on the way.",
      {"c": _int_stream(7, 150, 1, 300000), "rust": _int_stream(7, 150, 1, 300000),
       "go": _int_stream(7, 150, 1, 300000),
       "python": _int_stream(7, 60, 1, 200000)})

_algo("reverse-str", "Reverse a string", "str", {}, ascii_only=True,
      goal="Reverse the input string and print it. Handle every edge (empty, "
           "single char, spaces) exactly; look for cheap wins in how you copy.")
ALGORITHMS["reverse-str"]["workload"] = {
    l: _str_stream(7, 300) for l in LANG_EXT}

_algo("is-palindrome", "Palindrome check", "str", {}, ascii_only=True,
      goal="Print 1 if the input string is a palindrome else 0. Two pointers "
           "beat copying; handle empty and single-char inputs exactly.")
ALGORITHMS["is-palindrome"]["workload"] = {
    l: _str_stream(7, 300) for l in LANG_EXT}

_algo("rle", "Run-length encoding", "str", {}, ascii_only=True,
      goal="Run-length encode the input string (\"aaabcc\" -> \"a3b1c2\") and "
           "print it. Empty input prints an empty line. Keep it exact.")
ALGORITHMS["rle"]["workload"] = {l: _str_stream(7, 300) for l in LANG_EXT}
_algo("is-prime", "Primality test", "int", {"lo": 0, "hi": 10 ** 7},
      "Print 1 if n is prime else 0 as fast as possible. 6k±1 wheel and "
      "early exits beat naive trial division; output must stay exact.",
      {"c": _int_stream(7, 300, 0, 10 ** 7), "rust": _int_stream(7, 300, 0, 10 ** 7),
       "go": _int_stream(7, 300, 0, 10 ** 7),
       "python": _int_stream(7, 150, 0, 10 ** 6)})

_algo("int-sqrt", "Integer square root", "int", {"lo": 0, "hi": 10 ** 9},
      "Print floor(sqrt(n)) as fast as possible. Newton iteration or "
      "bit-by-bit construction beat the linear scan; output must stay exact.",
      {l: _int_stream(7, 300, 0, 10 ** 9) for l in LANG_EXT})

_algo("digital-root", "Digital root", "int", {"lo": 0, "hi": 10 ** 15},
      "Print the digital root of n (repeated digit sum to one digit; 0 -> 0). "
      "The O(1) formula 1 + (n-1) mod 9 beats the loop; output must stay exact.",
      {l: _int_stream(7, 400, 0, 10 ** 15) for l in LANG_EXT})

_algo("trailing-zeros", "Trailing zero bits", "int", {"lo": 0, "hi": 2 ** 31 - 1},
      "Print the number of trailing zero bits of n (n = 0 prints 0). Bit "
      "tricks (n & -n) beat the division loop; output must stay exact.",
      {l: _int_stream(7, 400, 0, 2 ** 31 - 1) for l in LANG_EXT})

_algo("omega", "Total prime factors", "int", {"lo": 1, "hi": 10 ** 7},
      "Print the number of prime factors of n counted with multiplicity "
      "(12 -> 3). Wheel factorization beats naive trial division; exact.",
      {"c": _int_stream(7, 300, 1, 10 ** 7), "rust": _int_stream(7, 300, 1, 10 ** 7),
       "go": _int_stream(7, 300, 1, 10 ** 7),
       "python": _int_stream(7, 150, 1, 10 ** 6)})

_algo("nth-prime", "Nth prime", "int", {"lo": 1, "hi": 2000},
      "Print the k-th prime (1-indexed: 1 -> 2). A sieve to a proven upper "
      "bound beats trial-dividing every candidate; output must stay exact.",
      {"c": _int_stream(7, 30, 1, 5000), "rust": _int_stream(7, 30, 1, 5000),
       "go": _int_stream(7, 30, 1, 5000),
       "python": _int_stream(7, 20, 1, 1000)})

_algo("levenshtein", "Levenshtein distance", "pair_str", {}, ascii_only=True,
      goal="Print the Levenshtein edit distance between the two input strings "
           "(insert/delete/substitute, cost 1 each). Banded DP and early "
           "exits beat the full matrix; keep it exact.")
ALGORITHMS["levenshtein"]["workload"] = {
    l: _pair_str_stream(7, 300, 40) for l in LANG_EXT}

_algo("lcs-length", "Longest common subsequence", "pair_str", {}, ascii_only=True,
      goal="Print the length of the longest common subsequence of the two "
           "input strings. Two-row DP is the baseline; look for cheaper wins.")
ALGORITHMS["lcs-length"]["workload"] = {
    l: _pair_str_stream(7, 300, 40) for l in LANG_EXT}

_algo("lpal-len", "Longest palindromic substring", "str", {}, ascii_only=True,
      goal="Print the length of the longest palindromic substring (empty "
           "input -> 0). Center expansion or Manacher beat scanning every "
           "substring; keep it exact.")
ALGORITHMS["lpal-len"]["workload"] = {l: _str_stream(7, 300) for l in LANG_EXT}

# kmp-prefix standard-KMP overrides: the baselines above reproduced a
# self-match bug in the original reference (pi[0] compared s[0] to s[0]).
# The reference is now corrected; these keep the baselines consistent.
_BASELINES["kmp-prefix"]["go"] = '''\
package main

import (
	"fmt"
	"os"
	"strings"
)

func main() {
	s := ""
	if len(os.Args) > 1 { s = os.Args[1] }
	v := []rune(s)
	pi := make([]int, len(v))
	for i := 1; i < len(v); i++ {
		j := pi[i-1]
		for j > 0 && v[i] != v[j] { j = pi[j-1] }
		if v[i] == v[j] { j++ }
		pi[i] = j
	}
	out := make([]string, len(pi))
	for i, x := range pi { out[i] = fmt.Sprint(x) }
	fmt.Println(strings.Join(out, " "))
}
'''
_BASELINES["kmp-prefix"]["javascript"] = '''\
const s = process.argv[2] || "";
const pi = new Array(s.length).fill(0);
for (let i = 1; i < s.length; i++) {
    let j = pi[i - 1];
    while (j > 0 && s[i] !== s[j]) j = pi[j - 1];
    if (s[i] === s[j]) j++;
    pi[i] = j;
}
console.log(pi.join(" "));
'''
_BASELINES["kmp-prefix"]["perl"] = '''\
my $s = defined $ARGV[0] ? $ARGV[0] : "";
my @pi = (0) x length($s);
for my $i (1 .. length($s) - 1) {
    my $j = $pi[$i - 1];
    while ($j > 0 && substr($s, $i, 1) ne substr($s, $j, 1)) { $j = $pi[$j - 1]; }
    $j++ if substr($s, $i, 1) eq substr($s, $j, 1);
    $pi[$i] = $j;
}
print join(" ", @pi), "\\n";
'''
_BASELINES["kmp-prefix"]["shell"] = '''\
s=${1:-}
n=${#s}
pi=()
[ -z "$s" ] && { echo ""; exit 0; }
pi[0]=0
for (( i = 1; i < n; i++ )); do
    j=${pi[i-1]}
    while [ "$j" -gt 0 ] && [ "${s:i:1}" != "${s:j:1}" ]; do j=${pi[j-1]}; done
    if [ "${s:i:1}" = "${s:j:1}" ]; then j=$((j + 1)); fi
    pi[$i]=$j
done
echo "${pi[*]}"
'''

_algo("kmp-prefix", "KMP prefix function", "str", {}, ascii_only=True,
      goal="Print the KMP prefix (failure) function of the input string as "
           "space-separated integers (pi[i] = longest proper prefix of "
           "s[0..i] that is also a suffix; empty input prints an empty line). "
           "The linear-time algorithm beats naive O(n^2) border checks.")
ALGORITHMS["kmp-prefix"]["workload"] = {l: _str_stream(7, 300) for l in LANG_EXT}

_algo("caesar-shift", "Caesar shift (+3)", "str", {},
      goal="Shift every letter of the input by 3 (wrapping within a-z and "
           "A-Z; all other characters unchanged) and print the result. Table "
           "lookups beat per-character branches.")
ALGORITHMS["caesar-shift"]["workload"] = {l: _str_stream(7, 300) for l in LANG_EXT}

_algo("happy-steps", "Happy number steps", "int", {"lo": 0, "hi": 10 ** 9},
      "Iterate n -> sum of squares of its digits; print the number of steps "
      "until the value is 1, or until a value repeats (whichever first). "
      "Small lookup tables beat recomputing digit sums; exact.",
      {l: _int_stream(7, 400, 0, 10 ** 9) for l in LANG_EXT})

_algo("modexp", "Modular exponentiation", "triple_int",
      {"alo": 0, "ahi": 10 ** 9, "blo": 0, "bhi": 10 ** 9,
       "mlo": 1, "mhi": 10 ** 9},
      "Compute a^b mod m (three arguments) as fast as possible: print one "
      "integer. Binary exponentiation is the baseline; watch overflow when "
      "squaring (m <= 10^9, use 64-bit).",
      {l: _triple_stream(7, 300, 0, 10 ** 9, 0, 10 ** 9, 1, 10 ** 9)
       for l in LANG_EXT})

_algo("max-subarray", "Maximum subarray sum", "intlist", {},
      goal="The input is one space-separated list of integers; print the "
           "maximum sum over all non-empty contiguous sublists. Kadane's "
           "O(n) beats the O(n^2) scan; exact.")
ALGORITHMS["max-subarray"]["workload"] = {
    "c": _intlist_stream(7, 200, 800), "rust": _intlist_stream(7, 200, 800),
    "go": _intlist_stream(7, 200, 800),
    "python": _intlist_stream(7, 100, 400)}

_algo("count-inversions", "Count inversions", "intlist", {},
      goal="The input is one space-separated list of integers; print the "
           "number of pairs i < j with a[i] > a[j]. Merge-sort counting "
           "O(n log n) beats the O(n^2) double loop; exact.")
ALGORITHMS["count-inversions"]["workload"] = {
    "c": _intlist_stream(7, 200, 1500), "rust": _intlist_stream(7, 200, 1500),
    "go": _intlist_stream(7, 200, 1500),
    "python": _intlist_stream(7, 100, 600)}


def list_algorithms() -> List[str]:
    return list(ALGORITHMS)


def list_languages() -> List[str]:
    return list(LANG_EXT)

# Explicit workloads name only a few languages (c/rust/go/python); every
# other supported language inherits the C scale unless it has its own
# entry, so make_project can never KeyError on workload[lang].
for _a in ALGORITHMS.values():
    wl = _a["workload"] or {}
    for _l in LANG_EXT:
        wl.setdefault(_l, wl.get("c"))
    _a["workload"] = wl
del _a, _l

# Shell (bash) tuning from the 2026-09-06 trial runs: C-scale workloads
# measured at 478 s total for the shell baselines — over the 600 s score
# cap once verify is added. The four heavy algos get scaled workloads;
# prime-count/fib-mod/sum-range also get scaled fuzz domains so a full
# 200-case gate stays inside the verify timeout. Fuzz intlist cases never
# exceed 120 elements, so list algos need no domain scaling.
ALGORITHMS["count-inversions"]["workload"]["shell"] = _intlist_stream(7, 40, 300)
ALGORITHMS["max-subarray"]["workload"]["shell"] = _intlist_stream(7, 60, 400)
ALGORITHMS["fib-mod"]["workload"]["shell"] = ["200000", "200000", "200000",
                                              "400000", "600000"]
ALGORITHMS["prime-count"]["domains"]["shell"] = {"hi": 100000}
ALGORITHMS["fib-mod"]["domains"]["shell"] = {"hi": 200000}
ALGORITHMS["sum-range"]["domains"]["shell"] = {"hi": 300000}

# --------------------------------------------------------------------------- #
# hand-written baselines: php, ruby, r, lua — no toolchain on THIS machine,
# so these are proven by the self-check gate on machines that have one.
# R and Lua (5.1 doubles) cannot hold a*b for modexp at m up to 10^9, so
# their projects get a scaled modexp domain (m <= 10^5 keeps every product
# < 10^10, exact in double precision).
# --------------------------------------------------------------------------- #

_BASELINES["prime-count"]["php"] = '''\
<?php
$n = isset($argv[1]) ? (int)$argv[1] : 0;
if ($n < 2) { echo "0\\n"; exit(0); }
$sieve = array_fill(0, $n, false);
for ($i = 2; $i * $i < $n; $i++) {
    if (!$sieve[$i]) for ($j = $i * $i; $j < $n; $j += $i) $sieve[$j] = true;
}
$c = 0;
for ($i = 2; $i < $n; $i++) if (!$sieve[$i]) $c++;
echo "$c\\n";
'''
_BASELINES["prime-count"]["ruby"] = '''\
n = (ARGV[0] || "0").to_i
(puts(0); exit 0) if n < 2
sieve = Array.new(n, false)
(2...n).each do |i|
  next if sieve[i]
  (i * i ... n).step(i) { |j| sieve[j] = true }
end
c = 0
(2...n).each { |i| c += 1 unless sieve[i] }
puts c
'''
_BASELINES["prime-count"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
n <- if (length(args) > 0) as.integer(args[1]) else 0L
if (n < 3) { cat(0, "\\n"); quit(save = "no") }
sieve <- rep(FALSE, n)
up <- as.integer(floor(sqrt(n - 1)))
if (up >= 2) {
  for (i in 2:up) {
    if (!sieve[i]) sieve[seq(i * i, n - 1, by = i)] <- TRUE
  }
}
cat(sum(!sieve[2:(n - 1)]), "\\n")
'''
_BASELINES["prime-count"]["lua"] = '''\
local n = tonumber(arg[1]) or 0
if n < 2 then print(0) return end
local sieve = {}
for i = 2, math.floor(math.sqrt(n - 1)) do
    if not sieve[i] then
        for j = i * i, n, i do sieve[j] = true end
    end
end
local c = 0
for i = 2, n - 1 do if not sieve[i] then c = c + 1 end end
print(c)
'''

_BASELINES["popcount"]["php"] = '''\
<?php
$x = isset($argv[1]) ? (int)$argv[1] : 0;
$c = 0;
while ($x) { $c += $x & 1; $x >>= 1; }
echo "$c\\n";
'''
_BASELINES["popcount"]["ruby"] = '''\
x = (ARGV[0] || "0").to_i
c = 0
while x > 0
  c += x & 1
  x >>= 1
end
puts c
'''
_BASELINES["popcount"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
x <- if (length(args) > 0) as.integer(args[1]) else 0L
c <- 0L
while (x > 0) { c <- c + (x %% 2L); x <- x %/% 2L }
cat(c, "\\n")
'''
_BASELINES["popcount"]["lua"] = '''\
local x = tonumber(arg[1]) or 0
local c = 0
while x > 0 do
    c = c + (x % 2)
    x = math.floor(x / 2)
end
print(c)
'''

_BASELINES["gcd"]["php"] = '''\
<?php
$a = isset($argv[1]) ? (int)$argv[1] : 0;
$b = isset($argv[2]) ? (int)$argv[2] : 0;
while ($b) { $t = $a % $b; $a = $b; $b = $t; }
echo "$a\\n";
'''
_BASELINES["gcd"]["ruby"] = '''\
a = (ARGV[0] || "0").to_i
b = (ARGV[1] || "0").to_i
while b != 0
  a, b = b, a % b
end
puts a
'''
_BASELINES["gcd"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
a <- if (length(args) > 0) as.integer(args[1]) else 0L
b <- if (length(args) > 1) as.integer(args[2]) else 0L
while (b != 0L) { t <- a %% b; a <- b; b <- t }
cat(a, "\\n")
'''
_BASELINES["gcd"]["lua"] = '''\
local a = tonumber(arg and arg[1]) or 0
local b = tonumber(arg and arg[2]) or 0
while b ~= 0 do
    local t = a % b
    a = b
    b = t
end
print(a)
'''

_BASELINES["fib-mod"]["php"] = '''\
<?php
$MOD = 1000000007;
$n = isset($argv[1]) ? (int)$argv[1] : 0;
$a = 0; $b = 1;
for ($i = 0; $i < $n; $i++) { $t = $a + $b; $a = $b; $b = $t % $MOD; }
echo "$a\\n";
'''
_BASELINES["fib-mod"]["ruby"] = '''\
MOD = 1000000007
n = (ARGV[0] || "0").to_i
a, b = 0, 1
n.times { a, b = b, (a + b) % MOD }
puts a
'''
_BASELINES["fib-mod"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
n <- if (length(args) > 0) as.integer(args[1]) else 0L
MOD <- 1000000007L
a <- 0L; b <- 1L
for (i in seq_len(n)) { t <- a + b; a <- b; b <- t %% MOD }
cat(a, "\\n")
'''
_BASELINES["fib-mod"]["lua"] = '''\
local MOD = 1000000007
local n = tonumber(arg[1]) or 0
local a, b = 0, 1
for i = 1, n do
    local t = a + b
    a = b
    b = t % MOD
end
print(a)
'''

_BASELINES["num-divisors"]["php"] = '''\
<?php
$n = isset($argv[1]) ? (int)$argv[1] : 0;
$c = 0;
for ($d = 1; $d * $d <= $n; $d++)
    if ($n % $d === 0) $c += ($d * $d === $n) ? 1 : 2;
echo "$c\\n";
'''
_BASELINES["num-divisors"]["ruby"] = '''\
n = (ARGV[0] || "0").to_i
c = 0
d = 1
while d * d <= n
  c += (d * d == n) ? 1 : 2 if n % d == 0
  d += 1
end
puts c
'''
_BASELINES["num-divisors"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
n <- if (length(args) > 0) as.integer(args[1]) else 0L
c <- 0L
d <- 1L
while (d * d <= n) {
  if (n %% d == 0L) c <- c + ifelse(d * d == n, 1L, 2L)
  d <- d + 1L
}
cat(c, "\\n")
'''
_BASELINES["num-divisors"]["lua"] = '''\
local n = tonumber(arg[1]) or 0
local c = 0
for d = 1, math.floor(math.sqrt(n)) do
    if n % d == 0 then
        c = c + (d * d == n and 1 or 2)
    end
end
print(c)
'''

_BASELINES["collatz-steps"]["php"] = '''\
<?php
$n = isset($argv[1]) ? (int)$argv[1] : 0;
$s = 0;
while ($n > 1) { if ($n % 2 === 0) $n /= 2; else $n = 3 * $n + 1; $s++; }
echo "$s\\n";
'''
_BASELINES["collatz-steps"]["ruby"] = '''\
n = (ARGV[0] || "0").to_i
s = 0
while n > 1
  n = (n % 2 == 0) ? n / 2 : 3 * n + 1
  s += 1
end
puts s
'''
_BASELINES["collatz-steps"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
n <- if (length(args) > 0) as.numeric(args[1]) else 0
s <- 0
while (n > 1) {
  n <- ifelse(n %% 2 == 0, n / 2, 3 * n + 1)
  s <- s + 1
}
cat(s, "\\n")
'''
_BASELINES["collatz-steps"]["lua"] = '''\
local n = tonumber(arg[1]) or 0
local s = 0
while n > 1 do
    if n % 2 == 0 then n = n / 2 else n = 3 * n + 1 end
    s = s + 1
end
print(s)
'''

_BASELINES["sum-range"]["php"] = '''\
<?php
$MOD = 1000000007;
$n = isset($argv[1]) ? (int)$argv[1] : 0;
$s = 0;
for ($i = 1; $i <= $n; $i++) $s = ($s + $i) % $MOD;
echo "$s\\n";
'''
_BASELINES["sum-range"]["ruby"] = '''\
MOD = 1000000007
n = (ARGV[0] || "0").to_i
s = 0
(1..n).each { |i| s = (s + i) % MOD }
puts s
'''
_BASELINES["sum-range"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
n <- if (length(args) > 0) as.integer(args[1]) else 0L
MOD <- 1000000007L
s <- 0L
for (i in seq_len(n)) s <- (s + i) %% MOD
cat(s, "\\n")
'''
_BASELINES["sum-range"]["lua"] = '''\
local MOD = 1000000007
local n = tonumber(arg[1]) or 0
local s = 0
for i = 1, n do s = (s + i) % MOD end
print(s)
'''

_BASELINES["reverse-str"]["php"] = '''\
<?php
$s = isset($argv[1]) ? $argv[1] : "";
echo strrev($s), "\\n";
'''
_BASELINES["reverse-str"]["ruby"] = '''\
s = ARGV[0] || ""
puts s.reverse
'''
_BASELINES["reverse-str"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
s <- if (length(args) > 0) args[1] else ""
ch <- strsplit(s, "")[[1]]
cat(paste(rev(ch), collapse = ""), "\\n")
'''
_BASELINES["reverse-str"]["lua"] = '''\
local s = arg[1] or ""
local out = {}
for i = #s, 1, -1 do out[#out + 1] = string.sub(s, i, i) end
print(table.concat(out))
'''

_BASELINES["is-palindrome"]["php"] = '''\
<?php
$s = isset($argv[1]) ? $argv[1] : "";
echo (strrev($s) === $s) ? "1\\n" : "0\\n";
'''
_BASELINES["is-palindrome"]["ruby"] = '''\
s = ARGV[0] || ""
puts(s.reverse == s ? 1 : 0)
'''
_BASELINES["is-palindrome"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
s <- if (length(args) > 0) args[1] else ""
ch <- strsplit(s, "")[[1]]
cat(ifelse(identical(ch, rev(ch)), "1", "0"), "\\n")
'''
_BASELINES["is-palindrome"]["lua"] = '''\
local s = arg[1] or ""
local i, j, ok = 1, #s, true
while i < j do
    if string.sub(s, i, i) ~= string.sub(s, j, j) then ok = false break end
    i = i + 1; j = j - 1
end
print(ok and 1 or 0)
'''

_BASELINES["rle"]["php"] = '''\
<?php
$s = isset($argv[1]) ? $argv[1] : "";
$out = "";
$i = 0; $n = strlen($s);
while ($i < $n) {
    $j = $i;
    while ($j < $n && $s[$j] === $s[$i]) $j++;
    $out .= $s[$i] . ($j - $i);
    $i = $j;
}
echo "$out\\n";
'''
_BASELINES["rle"]["ruby"] = '''\
s = ARGV[0] || ""
out = +""
i = 0
n = s.length
while i < n
  j = i
  j += 1 while j < n && s[j] == s[i]
  out << s[i] << (j - i).to_s
  i = j
end
puts out
'''
_BASELINES["rle"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
s <- if (length(args) > 0) args[1] else ""
ch <- strsplit(s, "")[[1]]
out <- character(0)
i <- 1L; n <- length(ch)
while (i <= n) {
  j <- i
  while (j < n && ch[j + 1L] == ch[i]) j <- j + 1L
  out <- c(out, ch[i], as.character(j - i + 1L))
  i <- j + 1L
}
cat(paste(out, collapse = ""), "\\n")
'''
_BASELINES["rle"]["lua"] = '''\
local s = arg[1] or ""
local out = {}
local i = 1
while i <= #s do
    local j = i
    while j < #s and string.sub(s, j + 1, j + 1) == string.sub(s, i, i) do j = j + 1 end
    out[#out + 1] = string.sub(s, i, i) .. tostring(j - i + 1)
    i = j + 1
end
print(table.concat(out))
'''

_BASELINES["is-prime"]["php"] = '''\
<?php
$n = isset($argv[1]) ? (int)$argv[1] : 0;
if ($n < 2) { echo "0\\n"; exit(0); }
for ($d = 2; $d * $d <= $n; $d++)
    if ($n % $d === 0) { echo "0\\n"; exit(0); }
echo "1\\n";
'''
_BASELINES["is-prime"]["ruby"] = '''\
n = (ARGV[0] || "0").to_i
(puts(0); exit 0) if n < 2
d = 2
while d * d <= n
  (puts(0); exit 0) if n % d == 0
  d += 1
end
puts 1
'''
_BASELINES["is-prime"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
n <- if (length(args) > 0) as.integer(args[1]) else 0L
if (n < 2) { cat("0\\n"); quit(save = "no") }
d <- 2L
while (d * d <= n) {
  if (n %% d == 0L) { cat("0\\n"); quit(save = "no") }
  d <- d + 1L
}
cat("1\\n")
'''
_BASELINES["is-prime"]["lua"] = '''\
local n = tonumber(arg[1]) or 0
if n < 2 then print(0) return end
for d = 2, math.floor(math.sqrt(n)) do
    if n % d == 0 then print(0) return end
end
print(1)
'''

_BASELINES["int-sqrt"]["php"] = '''\
<?php
$n = isset($argv[1]) ? (int)$argv[1] : 0;
$lo = 0; $hi = $n + 1;
while ($hi - $lo > 1) {
    $mid = intdiv($lo + $hi, 2);
    if ($mid * $mid <= $n) $lo = $mid; else $hi = $mid;
}
echo "$lo\\n";
'''
_BASELINES["int-sqrt"]["ruby"] = '''\
n = (ARGV[0] || "0").to_i
lo, hi = 0, n + 1
while hi - lo > 1
  mid = (lo + hi) / 2
  if mid * mid <= n then lo = mid else hi = mid end
end
puts lo
'''
_BASELINES["int-sqrt"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
n <- if (length(args) > 0) as.integer(args[1]) else 0L
lo <- 0L; hi <- n + 1L
while (hi - lo > 1L) {
  mid <- (lo + hi) %/% 2L
  if (as.double(mid) * mid <= n) lo <- mid else hi <- mid
}
cat(lo, "\\n")
'''
_BASELINES["int-sqrt"]["lua"] = '''\
local n = tonumber(arg[1]) or 0
local lo, hi = 0, n + 1
while hi - lo > 1 do
    local mid = math.floor((lo + hi) / 2)
    if mid * mid <= n then lo = mid else hi = mid end
end
print(lo)
'''

_BASELINES["digital-root"]["php"] = '''\
<?php
$n = isset($argv[1]) ? (int)$argv[1] : 0;
while ($n >= 10) {
    $s = 0;
    while ($n) { $s += $n % 10; $n = intdiv($n, 10); }
    $n = $s;
}
echo "$n\\n";
'''
_BASELINES["digital-root"]["ruby"] = '''\
n = (ARGV[0] || "0").to_i
while n >= 10
  s, m = 0, n
  while m > 0
    s += m % 10
    m /= 10
  end
  n = s
end
puts n
'''
_BASELINES["digital-root"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
n <- if (length(args) > 0) as.numeric(args[1]) else 0
while (n >= 10) {
  s <- 0; m <- n
  while (m > 0) { s <- s + (m %% 10); m <- floor(m / 10) }
  n <- s
}
cat(n, "\\n")
'''
_BASELINES["digital-root"]["lua"] = '''\
local n = tonumber(arg[1]) or 0
while n >= 10 do
    local s, m = 0, n
    while m > 0 do
        s = s + (m % 10)
        m = math.floor(m / 10)
    end
    n = s
end
print(n)
'''

_BASELINES["trailing-zeros"]["php"] = '''\
<?php
$n = isset($argv[1]) ? (int)$argv[1] : 0;
$c = 0;
while ($n !== 0 && $n % 2 === 0) { $c++; $n = intdiv($n, 2); }
echo "$c\\n";
'''
_BASELINES["trailing-zeros"]["ruby"] = '''\
n = (ARGV[0] || "0").to_i
c = 0
while n != 0 && n % 2 == 0
  c += 1
  n /= 2
end
puts c
'''
_BASELINES["trailing-zeros"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
n <- if (length(args) > 0) as.integer(args[1]) else 0L
c <- 0L
while (n != 0L && n %% 2 == 0L) { c <- c + 1L; n <- n %/% 2L }
cat(c, "\\n")
'''
_BASELINES["trailing-zeros"]["lua"] = '''\
local n = tonumber(arg[1]) or 0
local c = 0
while n ~= 0 and n % 2 == 0 do
    c = c + 1
    n = math.floor(n / 2)
end
print(c)
'''

_BASELINES["omega"]["php"] = '''\
<?php
$n = isset($argv[1]) ? (int)$argv[1] : 1;
$c = 0; $d = 2;
while ($d * $d <= $n) {
    while ($n % $d === 0) { $c++; $n = intdiv($n, $d); }
    $d++;
}
if ($n > 1) $c++;
echo "$c\\n";
'''
_BASELINES["omega"]["ruby"] = '''\
n = (ARGV[0] || "1").to_i
c, d = 0, 2
while d * d <= n
  while n % d == 0
    c += 1
    n /= d
  end
  d += 1
end
c += 1 if n > 1
puts c
'''
_BASELINES["omega"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
n <- if (length(args) > 0) as.integer(args[1]) else 1L
c <- 0L; d <- 2L
while (d * d <= n) {
  while (n %% d == 0L) { c <- c + 1L; n <- n %/% d }
  d <- d + 1L
}
if (n > 1L) c <- c + 1L
cat(c, "\\n")
'''
_BASELINES["omega"]["lua"] = '''\
local n = tonumber(arg[1]) or 1
local c, d = 0, 2
while d * d <= n do
    while n % d == 0 do c = c + 1; n = math.floor(n / d) end
    d = d + 1
end
if n > 1 then c = c + 1 end
print(c)
'''

_BASELINES["nth-prime"]["php"] = '''\
<?php
function is_p($x) {
    if ($x < 2) return false;
    for ($d = 2; $d * $d <= $x; $d++) if ($x % $d === 0) return false;
    return true;
}
$k = isset($argv[1]) ? (int)$argv[1] : 0;
$c = 0; $x = 1;
while ($c < $k) { $x++; if (is_p($x)) $c++; }
echo "$x\\n";
'''
_BASELINES["nth-prime"]["ruby"] = '''\
def is_p(x)
  return false if x < 2
  d = 2
  while d * d <= x
    return false if x % d == 0
    d += 1
  end
  true
end
k = (ARGV[0] || "0").to_i
c, x = 0, 1
while c < k
  x += 1
  c += 1 if is_p(x)
end
puts x
'''
_BASELINES["nth-prime"]["r"] = '''\
is_p <- function(x) {
  if (x < 2L) return(FALSE)
  d <- 2L
  while (d * d <= x) { if (x %% d == 0L) return(FALSE); d <- d + 1L }
  TRUE
}
args <- commandArgs(trailingOnly=TRUE)
k <- if (length(args) > 0) as.integer(args[1]) else 0L
c <- 0L; x <- 1L
while (c < k) { x <- x + 1L; if (is_p(x)) c <- c + 1L }
cat(x, "\\n")
'''
_BASELINES["nth-prime"]["lua"] = '''\
local function is_p(x)
    if x < 2 then return false end
    for d = 2, math.floor(math.sqrt(x)) do
        if x % d == 0 then return false end
    end
    return true
end
local k = tonumber(arg[1]) or 0
local c, x = 0, 1
while c < k do
    x = x + 1
    if is_p(x) then c = c + 1 end
end
print(x)
'''

_BASELINES["levenshtein"]["php"] = '''\
<?php
$a = isset($argv[1]) ? $argv[1] : "";
$b = isset($argv[2]) ? $argv[2] : "";
$la = strlen($a); $lb = strlen($b);
$prev = range(0, $lb);
for ($i = 1; $i <= $la; $i++) {
    $cur = array_fill(0, $lb + 1, 0);
    $cur[0] = $i;
    for ($j = 1; $j <= $lb; $j++) {
        $sub = $prev[$j - 1] + ($a[$i - 1] !== $b[$j - 1] ? 1 : 0);
        $cur[$j] = min($prev[$j] + 1, $cur[$j - 1] + 1, $sub);
    }
    $prev = $cur;
}
echo "$prev[$lb]\\n";
'''
_BASELINES["levenshtein"]["ruby"] = '''\
a = ARGV[0] || ""
b = ARGV[1] || ""
prev = (0..b.length).to_a
(1..a.length).each do |i|
  cur = [i] + Array.new(b.length, 0)
  (1..b.length).each do |j|
    sub = prev[j - 1] + (a[i - 1] != b[j - 1] ? 1 : 0)
    cur[j] = [prev[j] + 1, cur[j - 1] + 1, sub].min
  end
  prev = cur
end
puts prev[b.length]
'''
_BASELINES["levenshtein"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
a <- if (length(args) > 0) strsplit(args[1], "")[[1]] else character(0)
b <- if (length(args) > 1) strsplit(args[2], "")[[1]] else character(0)
la <- length(a); lb <- length(b)
prev <- as.integer(0:lb)
for (i in seq_len(la)) {
  cur <- integer(lb + 1)
  cur[1] <- i
  for (j in seq_len(lb)) {
    sub <- prev[j] + ifelse(a[i] != b[j], 1L, 0L)
    cur[j + 1] <- min(prev[j + 1] + 1L, cur[j] + 1L, sub)
  }
  prev <- cur
}
cat(prev[lb + 1], "\\n")
'''
_BASELINES["levenshtein"]["lua"] = '''\
local a = arg[1] or ""
local b = arg[2] or ""
local la, lb = #a, #b
local prev = {}
for j = 0, lb do prev[j + 1] = j end
for i = 1, la do
    local cur = { [1] = i }
    for j = 1, lb do
        local sub = prev[j] + (string.sub(a, i, i) ~= string.sub(b, j, j) and 1 or 0)
        local del = prev[j + 1] + 1
        local ins = cur[j] + 1
        cur[j + 1] = math.min(del, ins, sub)
    end
    prev = cur
end
print(prev[lb + 1])
'''

_BASELINES["lcs-length"]["php"] = '''\
<?php
$a = isset($argv[1]) ? $argv[1] : "";
$b = isset($argv[2]) ? $argv[2] : "";
$la = strlen($a); $lb = strlen($b);
$prev = array_fill(0, $lb + 1, 0);
for ($i = 1; $i <= $la; $i++) {
    $cur = array_fill(0, $lb + 1, 0);
    for ($j = 1; $j <= $lb; $j++) {
        if ($a[$i - 1] === $b[$j - 1]) $cur[$j] = $prev[$j - 1] + 1;
        else $cur[$j] = max($prev[$j], $cur[$j - 1]);
    }
    $prev = $cur;
}
echo "$prev[$lb]\\n";
'''
_BASELINES["lcs-length"]["ruby"] = '''\
a = ARGV[0] || ""
b = ARGV[1] || ""
prev = Array.new(b.length + 1, 0)
(1..a.length).each do |i|
  cur = Array.new(b.length + 1, 0)
  (1..b.length).each do |j|
    if a[i - 1] == b[j - 1] then cur[j] = prev[j - 1] + 1
    else cur[j] = [prev[j], cur[j - 1]].max end
  end
  prev = cur
end
puts prev[b.length]
'''
_BASELINES["lcs-length"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
a <- if (length(args) > 0) strsplit(args[1], "")[[1]] else character(0)
b <- if (length(args) > 1) strsplit(args[2], "")[[1]] else character(0)
la <- length(a); lb <- length(b)
prev <- integer(lb + 1)
for (i in seq_len(la)) {
  cur <- integer(lb + 1)
  for (j in seq_len(lb)) {
    if (a[i] == b[j]) cur[j + 1] <- prev[j] + 1L
    else cur[j + 1] <- max(prev[j + 1], cur[j])
  }
  prev <- cur
}
cat(prev[lb + 1], "\\n")
'''
_BASELINES["lcs-length"]["lua"] = '''\
local a = arg[1] or ""
local b = arg[2] or ""
local la, lb = #a, #b
local prev = {}
for j = 0, lb do prev[j + 1] = 0 end
for i = 1, la do
    local cur = { [1] = 0 }
    for j = 1, lb do
        if string.sub(a, i, i) == string.sub(b, j, j) then
            cur[j + 1] = prev[j] + 1
        else
            cur[j + 1] = math.max(prev[j + 1], cur[j])
        end
    end
    prev = cur
end
print(prev[lb + 1])
'''

_BASELINES["lpal-len"]["php"] = '''\
<?php
$s = isset($argv[1]) ? $argv[1] : "";
$n = strlen($s);
function is_pal($s, $i, $j) {
    while ($i < $j) if ($s[$i++] !== $s[$j--]) return false;
    return true;
}
$best = 0;
for ($i = 0; $i < $n; $i++) {
    if ($n - $i <= $best) break;
    for ($j = $i; $j < $n; $j++) {
        if ($j - $i + 1 > $best && is_pal($s, $i, $j)) $best = $j - $i + 1;
    }
}
echo "$best\\n";
'''
_BASELINES["lpal-len"]["ruby"] = '''\
s = ARGV[0] || ""
n = s.length
def is_pal(s, i, j)
  while i < j
    return false unless s[i] == s[j]
    i += 1; j -= 1
  end
  true
end
best = 0
(0...n).each do |i|
  if (n - i <= best) break
  (i...n).each do |j|
    best = j - i + 1 if j - i + 1 > best && is_pal(s, i, j)
  end
end
puts best
'''
_BASELINES["lpal-len"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
s <- if (length(args) > 0) strsplit(args[1], "")[[1]] else character(0)
n <- length(s)
is_pal <- function(i, j) {
  while (i < j) { if (!identical(s[i], s[j])) return(FALSE); i <- i + 1L; j <- j - 1L }
  TRUE
}
best <- 0L
for (i in seq_len(n)) {
  if ((n - i + 1L) <= best) break
  for (j in i:n) {
    if (j - i + 1L > best && is_pal(i, j)) best <- j - i + 1L
  }
}
cat(best, "\\n")
'''
_BASELINES["lpal-len"]["lua"] = '''\
local s = arg[1] or ""
local n = #s
local function is_pal(i, j)
    while i < j do
        if string.sub(s, i, i) ~= string.sub(s, j, j) then return false end
        i = i + 1; j = j - 1
    end
    return true
end
local best = 0
for i = 1, n do
    if n - i + 1 <= best then break end
    for j = i, n do
        if j - i + 1 > best and is_pal(i, j) then best = j - i + 1 end
    end
end
print(best)
'''

_BASELINES["kmp-prefix"]["php"] = '''\
<?php
$s = isset($argv[1]) ? $argv[1] : "";
$n = strlen($s);
$pi = array_fill(0, $n, 0);
for ($i = 1; $i < $n; $i++) {
    $j = $pi[$i - 1];
    while ($j > 0 && $s[$i] !== $s[$j]) $j = $pi[$j - 1];
    if ($s[$i] === $s[$j]) $j++;
    $pi[$i] = $j;
}
echo implode(" ", $pi), "\\n";
'''
_BASELINES["kmp-prefix"]["ruby"] = '''\
s = ARGV[0] || ""
n = s.length
pi = Array.new(n, 0)
(1...n).each do |i|
  j = pi[i - 1]
  j = pi[j - 1] while j > 0 && s[i] != s[j]
  j += 1 if s[i] == s[j]
  pi[i] = j
end
puts pi.join(" ")
'''
_BASELINES["kmp-prefix"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
s <- if (length(args) > 0) strsplit(args[1], "")[[1]] else character(0)
n <- length(s)
pi <- integer(n)
for (i in seq_len(n - 1)) {
  j <- pi[i]
  while (j > 0L && !identical(s[i + 1L], s[j + 1L])) j <- pi[j]
  if (identical(s[i + 1L], s[j + 1L])) j <- j + 1L
  pi[i + 1L] <- j
}
cat(paste(pi, collapse = " "), "\\n")
'''
_BASELINES["kmp-prefix"]["lua"] = '''\
local s = arg[1] or ""
local n = #s
local pi = {}
for i = 1, n do pi[i] = 0 end
for i = 2, n do
    local j = pi[i - 1]
    while j > 0 and string.sub(s, i, i) ~= string.sub(s, j + 1, j + 1) do
        j = pi[j]
    end
    if string.sub(s, i, i) == string.sub(s, j + 1, j + 1) then j = j + 1 end
    pi[i] = j
end
print(table.concat(pi, " "))
'''

_BASELINES["caesar-shift"]["php"] = '''\
<?php
$s = isset($argv[1]) ? $argv[1] : "";
$out = "";
for ($i = 0; $i < strlen($s); $i++) {
    $c = ord($s[$i]);
    if ($c >= 97 && $c <= 122) $out .= chr(97 + ($c - 97 + 3) % 26);
    elseif ($c >= 65 && $c <= 90) $out .= chr(65 + ($c - 65 + 3) % 26);
    else $out .= $s[$i];
}
echo "$out\\n";
'''
_BASELINES["caesar-shift"]["ruby"] = '''\
s = ARGV[0] || ""
out = +""
s.each_byte do |c|
  if c >= 97 && c <= 122 then out << (97 + (c - 97 + 3) % 26).chr
  elsif c >= 65 && c <= 90 then out << (65 + (c - 65 + 3) % 26).chr
  else out << c.chr end
end
puts out
'''
_BASELINES["caesar-shift"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
s <- if (length(args) > 0) args[1] else ""
raw <- charToRaw(s)
c <- as.integer(raw)
out <- c
lower <- c >= 97L & c <= 122L
upper <- c >= 65L & c <= 90L
out[lower] <- 97L + (c[lower] - 97L + 3L) %% 26L
out[upper] <- 65L + (c[upper] - 65L + 3L) %% 26L
cat(rawToChar(as.raw(out)), "\\n")
'''
_BASELINES["caesar-shift"]["lua"] = '''\
local s = arg[1] or ""
local out = {}
for i = 1, #s do
    local c = string.byte(s, i)
    if c >= 97 and c <= 122 then
        out[#out + 1] = string.char(97 + (c - 97 + 3) % 26)
    elseif c >= 65 and c <= 90 then
        out[#out + 1] = string.char(65 + (c - 65 + 3) % 26)
    else
        out[#out + 1] = string.char(c)
    end
end
print(table.concat(out))
'''

_BASELINES["happy-steps"]["php"] = '''\
<?php
$n = isset($argv[1]) ? (int)$argv[1] : 0;
$seen = [];
$steps = 0;
while ($n !== 1) {
    if (isset($seen[$n])) break;
    $seen[$n] = true;
    $s = 0; $m = $n;
    while ($m) { $d = $m % 10; $s += $d * $d; $m = intdiv($m, 10); }
    $n = $s;
    $steps++;
}
echo "$steps\\n";
'''
_BASELINES["happy-steps"]["ruby"] = '''\
n = (ARGV[0] || "0").to_i
seen = {}
steps = 0
while n != 1
  if (n %in% seen) break
  seen[n] = true
  s, m = 0, n
  while m > 0
    d = m % 10
    s += d * d
    m /= 10
  end
  n = s
  steps += 1
end
puts steps
'''
_BASELINES["happy-steps"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
n <- if (length(args) > 0) as.integer(args[1]) else 0L
seen <- integer(0)
steps <- 0L
while (n != 1L) {
  if (n %in% seen) break
  seen <- c(seen, n)
  s <- 0L; m <- n
  while (m > 0L) { d <- m %% 10L; s <- s + d * d; m <- m %/% 10L }
  n <- s
  steps <- steps + 1L
}
cat(steps, "\\n")
'''
_BASELINES["happy-steps"]["lua"] = '''\
local n = tonumber(arg[1]) or 0
local seen = {}
local steps = 0
while n ~= 1 do
    if seen[n] then break end
    seen[n] = true
    local s, m = 0, n
    while m > 0 do
        local d = m % 10
        s = s + d * d
        m = math.floor(m / 10)
    end
    n = s
    steps = steps + 1
end
print(steps)
'''

_BASELINES["modexp"]["php"] = '''\
<?php
$a = isset($argv[1]) ? (int)$argv[1] : 0;
$b = isset($argv[2]) ? (int)$argv[2] : 0;
$m = isset($argv[3]) ? (int)$argv[3] : 1;
if ($m === 1) { echo "0\\n"; exit(0); }
$r = 1 % $m;
$a %= $m;
while ($b) {
    if ($b & 1) $r = ($r * $a) % $m;
    $a = ($a * $a) % $m;
    $b >>= 1;
}
echo "$r\\n";
'''
_BASELINES["modexp"]["ruby"] = '''\
a = (ARGV[0] || "0").to_i
b = (ARGV[1] || "0").to_i
m = (ARGV[2] || "1").to_i
(puts(0); exit 0) if m == 1
r = 1 % m
a %= m
while b > 0
  r = (r * a) % m if b.odd?
  a = (a * a) % m
  b >>= 1
end
puts r
'''
_BASELINES["modexp"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
a <- if (length(args) > 0) as.numeric(args[1]) else 0
b <- if (length(args) > 1) as.integer(args[2]) else 0L
m <- if (length(args) > 2) as.numeric(args[3]) else 1
if (m == 1) { cat("0\\n"); quit(save = "no") }
r <- 1 %% m
a <- a %% m
while (b > 0L) {
  if (b %% 2L == 1L) r <- (r * a) %% m
  a <- (a * a) %% m
  b <- b %/% 2L
}
cat(r, "\\n")
'''
_BASELINES["modexp"]["lua"] = '''\
local a = tonumber(arg and arg[1]) or 0
local b = tonumber(arg and arg[2]) or 0
local m = tonumber(arg and arg[3]) or 1
if m == 1 then print(0) return end
local r = 1 % m
a = a % m
while b > 0 do
    if b % 2 == 1 then r = (r * a) % m end
    a = (a * a) % m
    b = math.floor(b / 2)
end
print(r)
'''

_BASELINES["max-subarray"]["php"] = '''\
<?php
$nums = isset($argv[1]) ? array_filter(explode(" ", $argv[1])) : [];
if (count($nums) === 0) { echo "0\\n"; exit(0); }
$best = (int)$nums[0];
$n = count($nums);
for ($i = 0; $i < $n; $i++) {
    $sum = 0;
    for ($j = $i; $j < $n; $j++) {
        $sum += (int)$nums[$j];
        if ($sum > $best) $best = $sum;
    }
}
echo "$best\\n";
'''
_BASELINES["max-subarray"]["ruby"] = '''\
nums = (ARGV[0] || "").split(" ").reject { |t| t.empty? }.map(&:to_i)
(puts(0); exit 0) if nums.empty?
best = nums[0]
nums.each_index do |i|
  sum = 0
  (i...nums.length).each do |j|
    sum += nums[j]
    best = sum if sum > best
  end
end
puts best
'''
_BASELINES["max-subarray"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
nums <- if (length(args) > 0) as.integer(strsplit(args[1], "\\\\s+")[[1]]) else integer(0)
if (length(nums) == 0L) { cat("0\\n"); quit(save = "no") }
n <- length(nums)
best <- nums[1]
for (i in seq_len(n)) {
  s <- 0L
  for (j in i:n) {
    s <- s + nums[j]
    if (s > best) best <- s
  }
}
cat(best, "\\n")
'''
_BASELINES["max-subarray"]["lua"] = '''\
local s = arg[1] or ""
local nums = {}
for tok in string.gmatch(s, "%S+") do nums[#nums + 1] = tonumber(tok) end
if #nums == 0 then print(0) return end
local best = nums[1]
for i = 1, #nums do
    local sum = 0
    for j = i, #nums do
        sum = sum + nums[j]
        if sum > best then best = sum end
    end
end
print(best)
'''

_BASELINES["count-inversions"]["php"] = '''\
<?php
$nums = isset($argv[1]) ? array_filter(explode(" ", $argv[1])) : [];
$c = 0;
$n = count($nums);
for ($i = 0; $i < $n; $i++)
    for ($j = $i + 1; $j < $n; $j++)
        if ((int)$nums[$i] > (int)$nums[$j]) $c++;
echo "$c\\n";
'''
_BASELINES["count-inversions"]["ruby"] = '''\
nums = (ARGV[0] || "").split(" ").reject { |t| t.empty? }.map(&:to_i)
c = 0
(0...nums.length).each do |i|
  ((i + 1)...nums.length).each do |j|
    c += 1 if nums[i] > nums[j]
  end
end
puts c
'''
_BASELINES["count-inversions"]["r"] = '''\
args <- commandArgs(trailingOnly=TRUE)
nums <- if (length(args) > 0) as.integer(strsplit(args[1], "\\\\s+")[[1]]) else integer(0)
n <- length(nums)
c <- 0L
for (i in seq_len(n - 1)) {
  for (j in (i + 1L):n) { if (nums[i] > nums[j]) c <- c + 1L }
}
cat(c, "\\n")
'''
_BASELINES["count-inversions"]["lua"] = '''\
local s = arg[1] or ""
local nums = {}
for tok in string.gmatch(s, "%S+") do nums[#nums + 1] = tonumber(tok) end
local c = 0
for i = 1, #nums - 1 do
    for j = i + 1, #nums do
        if nums[i] > nums[j] then c = c + 1 end
    end
end
print(c)
'''

# R and Lua: modexp domain scaled so a*b stays < 10^10 (exact in double).
ALGORITHMS["modexp"]["domains"]["r"] = {"mhi": 10 ** 5}
ALGORITHMS["modexp"]["domains"]["lua"] = {"mhi": 10 ** 5}
ALGORITHMS["modexp"]["workload"]["r"] = _triple_stream(7, 60, 0, 10 ** 9, 0, 10 ** 9, 1, 10 ** 5)
ALGORITHMS["modexp"]["workload"]["lua"] = _triple_stream(7, 60, 0, 10 ** 9, 0, 10 ** 9, 1, 10 ** 5)

# Per-language baselines that live in their own module (kept separate so the
# big factory.py stays readable).  Loaded when present; missing = no-op, so
# the registry never breaks on a partial checkout.
try:
    from .factory_langs import EXTRA_BASELINES
    for _algo, _langs in EXTRA_BASELINES.items():
        _BASELINES.setdefault(_algo, {}).update(_langs)
except ImportError:
    pass


# --------------------------------------------------------------------------- #
# harness templates
# --------------------------------------------------------------------------- #

def _build_script(probes: List[Tuple[str, List[str]]]) -> str:
    """Emit a build.py that tries each (toolchain, argv) pair in order.

    argv items may contain {cand} / {art} placeholders. First toolchain found
    on PATH is used; its compile output decides success (stderr surfaced)."""
    rows = ",\n".join(
        f"    ({json.dumps(probe)}, {json.dumps(argv)})" for probe, argv in probes)
    return (
        "#!/usr/bin/env python3\n"
        "import shutil, subprocess, sys\n"
        f"PROBES = [\n{rows}\n]\n"
        "cand, art = sys.argv[1], sys.argv[2]\n"
        "for probe, argv in PROBES:\n"
        "    if not shutil.which(probe):\n"
        "        continue\n"
        "    r = subprocess.run([a.format(cand=cand, art=art) for a in argv],\n"
        "                       capture_output=True)\n"
        "    if r.returncode == 0:\n"
        "        print(\"OK\")\n"
        "        sys.exit(0)\n"
        "    sys.stderr.write(r.stderr.decode(errors=\"replace\")[:4000])\n"
        "    sys.exit(1)\n"
        "sys.stderr.write(\"no toolchain found for this language\\n\")\n"
        "sys.exit(1)\n"
    )


# Per-interpreter syntax probe so a broken candidate fails the BUILD step
# (where the deterministic autofix nudge fires) instead of surfacing as a
# runtime crash in verify/score — a small-model guard, not just a lint.
_SYNTAX_PROBE: Dict[str, List[str]] = {
    "ruby": ["-c", "%s"],
    "php": ["-l", "%s"],
    "perl": ["-c", "%s"],
    "python": ["-c", "import ast,sys;ast.parse(open(sys.argv[1]).read())", "%s"],
    "node": ["--check", "%s"],
    "bash": ["-n", "%s"],
    "sh": ["-n", "%s"],
    "lua": ["-e", "assert(loadfile((...)))", "%s"],
    "lua5.4": ["-e", "assert(loadfile((...)))", "%s"],
    "lua5.3": ["-e", "assert(loadfile((...)))", "%s"],
    "luajit": ["-e", "assert(loadfile((...)))", "%s"],
    "Rscript": ["--vanilla", "-e",
                "invisible(parse(file=commandArgs(trailingOnly=TRUE)[1]))", "%s"],
}


def _interp_script(interps: List[str]) -> str:
    """Emit a build.py for an interpreted language: copy the candidate, make
    it executable, and insure the shebang (the build step owns executability,
    the model owns logic). Shebang points at the first interpreter found.

    When the chosen interpreter has a known syntax probe, the build runs it
    on the artifact first and fails on a parse error — so a broken candidate
    is caught at build (and auto-nudged) rather than crashing at runtime in
    verify/score."""
    probe_map = f"PROBE = {json.dumps({k: v for k, v in _SYNTAX_PROBE.items()})}\n"
    return (
        "#!/usr/bin/env python3\n"
        "import shutil, stat, subprocess, sys\n"
        f"CANDIDATES = {json.dumps(interps)}\n"
        f"{probe_map}"
        "cand, art = sys.argv[1], sys.argv[2]\n"
        "shutil.copyfile(cand, art)\n"
        "with open(art, \"rb\") as f:\n"
        "    first = f.readline(64)\n"
        "interp = next((c for c in CANDIDATES if shutil.which(c)), None)\n"
        "if interp is None:\n"
        "    sys.stderr.write(\"no interpreter found: \" + \",\".join(CANDIDATES))\n"
        "    sys.exit(1)\n"
        "if not first.startswith(b\"#!\"):\n"
        "    data = open(cand, \"rb\").read()\n"
        "    with open(art, \"wb\") as f:\n"
        "        f.write(b\"#!/usr/bin/env \" + interp.encode() + b\"\\n\")\n"
        "        f.write(data)\n"
        "st = __import__(\"os\").stat(art)\n"
        "__import__(\"os\").chmod(art, st.st_mode | stat.S_IEXEC)\n"
        "probe = PROBE.get(interp)\n"
        "if probe:\n"
        "    argv = [interp] + [a.replace(\"%s\", art) for a in probe]\n"
        "    pr = subprocess.run(argv, capture_output=True)\n"
        "    if pr.returncode != 0:\n"
        "        sys.stderr.write((pr.stderr or b\"\").decode(\"utf-8\", \"replace\"))\n"
        "        sys.exit(1)\n"
        "print(\"OK\")\n"
    )


# Java/Kotlin/Scala/C#/TypeScript/Zig produce no directly-executable file:
# the build compiles into a persistent side-dir and emits a tiny sh wrapper
# as the artifact, keeping the harness contract (artifact runs directly with
# argv) uniform across all 24 languages.
_JAVA_BUILD = '''\
#!/usr/bin/env python3
import glob, os, shutil, stat, subprocess, sys
cand, art = sys.argv[1], sys.argv[2]
if not shutil.which("javac"):
    sys.stderr.write("no toolchain found: javac\\n"); sys.exit(1)
td = art + ".classes"
os.makedirs(td, exist_ok=True)
for old in glob.glob(os.path.join(td, "*")):
    os.remove(old)
src = os.path.join(td, "Main.java")
shutil.copyfile(cand, src)
r = subprocess.run(["javac", "-d", td, src], capture_output=True)
if r.returncode != 0:
    sys.stderr.write(r.stderr.decode(errors="replace")[:4000]); sys.exit(1)
# entry point: the class with a main method (usually Main)
entry = None
for clsfile in sorted(glob.glob(os.path.join(td, "*.class"))):
    clsname = os.path.basename(clsfile)[:-len(".class")]
    r = subprocess.run(["javap", "-p", clsname], cwd=td, capture_output=True, text=True)
    if "static void main" in r.stdout:
        entry = clsname; break
if entry is None:
    sys.stderr.write("no class with a main method found\\n"); sys.exit(1)
jar = art + ".jar"
r = subprocess.run(["jar", "cfe", jar, entry, "-C", td, "."], capture_output=True)
if r.returncode != 0:
    sys.stderr.write(r.stderr.decode(errors="replace")[:4000]); sys.exit(1)
with open(art, "w") as f:
    f.write("#!/bin/sh\\nexec java -jar %r \\"$@\\"\\n" % jar)
os.chmod(art, 0o755)
print("OK")
'''

_KOTLIN_BUILD = '''\
#!/usr/bin/env python3
import os, shutil, subprocess, sys
cand, art = sys.argv[1], sys.argv[2]
if not shutil.which("kotlinc"):
    sys.stderr.write("no toolchain found: kotlinc\\n"); sys.exit(1)
jar = art + ".jar"
r = subprocess.run(["kotlinc", cand, "-include-runtime", "-d", jar],
                   capture_output=True)
if r.returncode != 0:
    sys.stderr.write(r.stderr.decode(errors="replace")[:4000]); sys.exit(1)
with open(art, "w") as f:
    f.write("#!/bin/sh\\nexec java -jar %r \\"$@\\"\\n" % jar)
os.chmod(art, 0o755)
print("OK")
'''

_SCALA_BUILD = '''\
#!/usr/bin/env python3
import glob, os, shutil, subprocess, sys
cand, art = sys.argv[1], sys.argv[2]
if not shutil.which("scalac"):
    sys.stderr.write("no toolchain found: scalac\\n"); sys.exit(1)
td = art + ".classes"
os.makedirs(td, exist_ok=True)
r = subprocess.run(["scalac", cand, "-d", td], capture_output=True)
if r.returncode != 0:
    sys.stderr.write(r.stderr.decode(errors="replace")[:4000]); sys.exit(1)
entry = os.path.basename(cand)[:-len(".scala")]
with open(art, "w") as f:
    f.write("#!/bin/sh\\nexec scala -cp %r %s \\"$@\\"\\n" % (td, entry))
os.chmod(art, 0o755)
print("OK")
'''

_CSHARP_BUILD = '''\
#!/usr/bin/env python3
import os, shutil, subprocess, sys
cand, art = sys.argv[1], sys.argv[2]
dll = art + ".dll"
if shutil.which("csc") or shutil.which("mcs"):
    csc = "csc" if shutil.which("csc") else "mcs"
    r = subprocess.run([csc, cand, "-out:" + dll], capture_output=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr.decode(errors="replace")[:4000]); sys.exit(1)
    with open(art, "w") as f:
        f.write("#!/bin/sh\\nexec mono %r \\"$@\\"\\n" % dll)
else:
    if not shutil.which("dotnet"):
        sys.stderr.write("no toolchain found for C# (csc/mcs/dotnet)\\n"); sys.exit(1)
    td = art + ".proj"
    os.makedirs(td, exist_ok=True)
    shutil.copyfile(cand, os.path.join(td, "Main.cs"))
    with open(os.path.join(td, "app.csproj"), "w") as f:
        f.write('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                '<OutputType>Exe</OutputType><TargetFramework>net8.0</'
                'TargetFramework></PropertyGroup></Project>')
    r = subprocess.run(["dotnet", "build", "-c", "Release", "-o", td,
                        os.path.join(td, "app.csproj")], capture_output=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr.decode(errors="replace")[:4000]); sys.exit(1)
    with open(art, "w") as f:
        f.write("#!/bin/sh\\nexec dotnet %r \\"$@\\"\\n" % os.path.join(td, "app.dll"))
os.chmod(art, 0o755)
print("OK")
'''

_TYPESCRIPT_BUILD = '''\
#!/usr/bin/env python3
import os, shutil, subprocess, sys
cand, art = sys.argv[1], sys.argv[2]
if not shutil.which("tsc"):
    sys.stderr.write("no toolchain found: tsc\\n"); sys.exit(1)
td = art + ".jsd"
os.makedirs(td, exist_ok=True)
r = subprocess.run(["tsc", cand, "--outDir", td, "--target", "es2020",
                    "--module", "commonjs"], capture_output=True)
if r.returncode != 0:
    sys.stderr.write(r.stderr.decode(errors="replace")[:4000]); sys.exit(1)
js = os.path.join(td, os.path.basename(cand)[:-len(".ts")] + ".js")
with open(art, "w") as f:
    f.write("#!/bin/sh\\nexec node %r \\"$@\\"\\n" % js)
os.chmod(art, 0o755)
print("OK")
'''

_ZIG_BUILD = '''\
#!/usr/bin/env python3
import os, shutil, subprocess, sys
cand, art = sys.argv[1], sys.argv[2]
if not shutil.which("zig"):
    sys.stderr.write("no toolchain found: zig\\n"); sys.exit(1)
r = subprocess.run(["zig", "build-exe", cand], capture_output=True,
                   cwd=os.path.dirname(cand) or ".")
if r.returncode != 0:
    sys.stderr.write(r.stderr.decode(errors="replace")[:4000]); sys.exit(1)
out = os.path.join(os.path.dirname(cand) or ".",
                   os.path.basename(cand)[:-len(".zig")])
shutil.move(out, art)
print("OK")
'''

_BUILD_SCRIPTS: Dict[str, str] = {
    "c": _build_script([
        ("gcc", ["gcc", "-O2", "-Werror=implicit-function-declaration",
                 "-o", "{art}", "{cand}"]),
        ("cc", ["cc", "-O2", "-Werror=implicit-function-declaration",
                "-o", "{art}", "{cand}"]),
        ("clang", ["clang", "-O2", "-Werror=implicit-function-declaration",
                   "-o", "{art}", "{cand}"]),
    ]),
    "cpp": _build_script([
        ("g++", ["g++", "-O2", "-std=c++17", "-o", "{art}", "{cand}"]),
        ("c++", ["c++", "-O2", "-std=c++17", "-o", "{art}", "{cand}"]),
        ("clang++", ["clang++", "-O2", "-std=c++17", "-o", "{art}", "{cand}"]),
    ]),
    "cuda": _build_script([
        ("nvcc", ["nvcc", "-O2", "-o", "{art}", "{cand}"]),
    ]),
    "python": _interp_script(["python3"]),
    "java": _JAVA_BUILD,
    "javascript": _interp_script(["node"]),
    "typescript": _TYPESCRIPT_BUILD,
    "csharp": _CSHARP_BUILD,
    "go": _build_script([("go", ["go", "build", "-o", "{art}", "{cand}"])]),
    "rust": _build_script([("rustc", ["rustc", "-O", "-o", "{art}", "{cand}"])]),
    "kotlin": _KOTLIN_BUILD,
    "swift": _build_script([
        ("swiftc", ["swiftc", "-O", "-o", "{art}", "{cand}"]),
    ]),
    "php": _interp_script(["php"]),
    "ruby": _interp_script(["ruby"]),
    "r": _interp_script(["Rscript", "R"]),
    "zig": _ZIG_BUILD,
    "scala": _SCALA_BUILD,
    "dart": _build_script([
        ("dart", ["dart", "compile", "exe", "-o", "{art}", "{cand}"]),
    ]),
    "haskell": _build_script([
        ("ghc", ["ghc", "-O2", "-o", "{art}", "{cand}"]),
    ]),
    "lua": _interp_script(["lua", "lua5.4", "lua5.3", "luajit"]),
    "perl": _interp_script(["perl"]),
    "shell": _interp_script(["bash", "sh"]),
    "d": _build_script([
        ("dmd", ["dmd", "-O", "-of={art}", "{cand}"]),
        ("ldc2", ["ldc2", "-O2", "-of={art}", "{cand}"]),
        ("gdc", ["gdc", "-O2", "-o", "{art}", "{cand}"]),
    ]),
}

_SCORE_TPL = '''\
#!/usr/bin/env python3
"""Score harness: times the artifact on a fixed deterministic workload."""
import subprocess, sys, time

ARTIFACT = sys.argv[1] if len(sys.argv) > 1 else "program"
WORKLOAD = __WORKLOAD__


def main():
    t0 = time.perf_counter()
    for argv in WORKLOAD:
        r = subprocess.run([ARTIFACT] + list(argv), capture_output=True, timeout=60)
        if r.returncode != 0:
            print(f"SCORE FAIL argv={argv} exit={r.returncode}", file=sys.stderr)
            sys.exit(1)
    dt_ms = (time.perf_counter() - t0) * 1000.0
    print(f"time_ms={dt_ms:.1f}")


main()
'''


def _stable_seed(algo_key: str, lang: str) -> int:
    return int(hashlib.sha256(f"{algo_key}-{lang}".encode()).hexdigest()[:8], 16)


# --------------------------------------------------------------------------- #
# project generation
# --------------------------------------------------------------------------- #

def _contract_text(algo_key: str, cases: List[Dict[str, Any]]) -> str:
    """Explicit I/O contract for every factory project. The model must never
    guess where input comes from — live incident: candidates read stdin
    while the gate passed argv, producing 36 straight generations of empty
    output with an empty champion block and no example to anchor on."""
    goal = ALGORITHMS[algo_key]["goal"]
    ex = ""
    if cases:
        c0 = cases[0]
        args = " ".join(str(a) for a in c0.get("argv", []))
        exp = str(c0.get("expected", "")).rstrip("\n")
        ex = (f"\nExample: `program {args}` must print exactly `{exp}`"
              " plus one newline.")
    return (
        "I/O PROTOCOL — read the input(s) from command-line arguments, "
        "starting at argument 1 (argv[1] in C, os.Args[1] in Go, "
        "std::env::args().nth(1) in Rust, sys.argv[1] in Python). NEVER "
        "read from stdin. Print exactly one line to stdout: the result "
        f"followed by a newline.\n\nTASK — {goal}" + ex
    )

# --------------------------------------------------------------------------- #
# per-language timeouts from empirical trial runs on the campaign machine
# (2026-09-06: every baseline built and run on its full workload; values are
# the measured worst case with margin). Hard caps by policy: 120 s (2 min)
# build, 600 s (10 min) execution. Languages absent from a table get the cap.
# --------------------------------------------------------------------------- #
LANG_BUILD_TIMEOUT: Dict[str, float] = {
    # measured worst-case baseline build (all 25 algos): c/cpp ~0.1 s,
    # go ~0.2 s, rust ~0.3 s, cuda ~1 s (nvcc), interpreted ~0.03 s —
    # values carry 10-100x margin for cold caches and grown candidates.
    "c": 30.0, "cpp": 30.0, "cuda": 60.0, "python": 15.0,
    "javascript": 15.0, "go": 60.0, "rust": 60.0,
    "perl": 15.0, "shell": 15.0,
}
LANG_CASE_TIMEOUT: Dict[str, float] = {
    # measured worst single run at C-scale workloads: compiled ~0.01 s,
    # python ~0.07 s, javascript ~0.11 s, perl ~0.21 s, shell 13 s
    # (fib-mod n=3e6 — scaled down for the shell project). Fuzz cases are
    # no larger than the workload domain edges.
    "c": 10.0, "cpp": 10.0, "cuda": 10.0, "python": 30.0,
    "javascript": 10.0, "go": 10.0, "rust": 10.0,
    "perl": 20.0, "shell": 60.0,
}

def make_project(algo_key: str, lang: str, n_cases: int = 200,
                 seed: Optional[int] = None,
                 build_timeout: Optional[float] = None,
                 case_timeout: Optional[float] = None) -> Dict[str, Any]:
    """Generate spec + files for one (algorithm, language) project.

    Returns {"id", "spec", "files"} — or raises ValueError/KeyError with a
    precise message. Reference outputs are computed from the trusted Python
    reference; the baseline is NOT trusted until check_project proves it.
    Timeouts: explicit args win, else per-language empirical table, else the
    hard caps (120 s build / 600 s execution)."""
    algo = ALGORITHMS.get(algo_key)
    if algo is None:
        raise KeyError(f"unknown algorithm {algo_key!r} (have {list_algorithms()})")
    if lang not in LANG_EXT:
        raise KeyError(f"unknown language {lang!r} (have {list_languages()})")

    seed = _stable_seed(algo_key, lang) if seed is None else seed
    family = algo["family"]
    domain = dict(algo["domain"])
    # Per-language fuzz-domain override: a naive Python baseline cannot chew
    # C-sized domains (prime-count at n=10^6 is ~30-90s/case in pure Python),
    # so the python project gets a scaled domain — same pattern as the
    # per-language workload scaling.
    override = algo.get("domains", {}).get(lang)
    if override:
        domain.update(override)
    cases = F.gen_cases(family, seed=seed, n=n_cases,
                        ascii_only=algo.get("ascii_only", False), **domain)

    # Reference outputs: run the trusted Python reference on every case.
    with tempfile.TemporaryDirectory(prefix="kaisen-factory-ref-") as td:
        ref = Path(td) / "ref.py"
        src = algo["ref"] if algo["ref"].startswith("#!") else "#!/usr/bin/env python3\n" + algo["ref"]
        ref.write_text(src, encoding="utf-8")
        ref.chmod(0o755)
        cases = F.compute_expected(ref, cases, timeout=60.0)

    # Timeouts: explicit override > per-language empirical table > hard cap.
    bt = build_timeout if build_timeout is not None \
        else LANG_BUILD_TIMEOUT.get(lang, 120.0)
    ct = case_timeout if case_timeout is not None \
        else LANG_CASE_TIMEOUT.get(lang, 10.0)
    bt = min(120.0, max(5.0, bt))          # policy cap: 2 min compile
    ct = min(600.0, max(1.0, ct))          # policy cap: 10 min execution
    verify_timeout = min(600.0, ct * n_cases * 1.5 + 30.0)

    workload = algo["workload"][lang]
    ext = LANG_EXT[lang]
    pid = f"{algo_key}-{lang}"
    files: Dict[str, str] = {
        f"original.{ext}": algo["baselines"][lang],
        "harness/build.py": _BUILD_SCRIPTS[lang],
        "harness/fuzz_verify.py": SHARED_FUZZ_VERIFY.read_text(encoding="utf-8"),
        "harness/score.py": _SCORE_TPL.replace(
            "__WORKLOAD__", json.dumps(workload)),
        "fuzz_cases.json": json.dumps({
            "family": family, "seed": seed, "n": n_cases,
            "compare": algo["compare"], "case_timeout": ct,
            "domain": domain, "cases": cases,
        }, indent=1),
    }

    spec: Dict[str, Any] = {
        "id": pid,
        "name": f"{algo['name']} ({LANG_LABEL[lang]})",
        "description": (f"Factory project: {algo['goal']} Language: {LANG_LABEL[lang]}. "
                        f"Fuzz gate: {n_cases} seeded cases vs reference."),
        "language": lang,
        "artifact_name": "program",
        "steps": {
            "build": {"program": "harness/build.py",
                      "args": ["{candidate}", "{artifact}"], "timeout": bt},
            "verify": [{"program": "harness/fuzz_verify.py",
                        "args": ["{artifact}"], "timeout": verify_timeout}],
            "score": [{"program": "harness/score.py",
                       "args": ["{artifact}"], "timeout": 600.0,
                       "parse": [{"type": "regex",
                                  "pattern": "time_ms=(?P<time_ms>[\\d.]+)"}]}],
        },
        "metrics": {"time_ms": {
            "label": f"Workload wall time ({len(workload)} runs)",
            "unit": "ms", "direction": "lower", "weight": 1}},
        "telemetry": {"enabled": True, "progress_token": "KAISEN_PROGRESS",
                      "live_fields": ["time_ms"]},
        "select": {"hysteresis": 1.0001},
        "guardrails": {"enabled": True, "allow_extra": [], "deny_extra": []},
        "prompts": {"goal": algo["goal"]},
        "data": {"baseline_source": f"original.{ext}",
                 "contract_text": _contract_text(algo_key, cases),
                 "fuzz": {"family": family, "seed": seed, "n": n_cases,
                          "compare": algo["compare"], **domain}},
    }
    errors = validate_spec(spec)
    if errors:
        raise ValueError(f"{pid}: invalid spec: {errors}")
    return {"id": pid, "spec": spec, "files": files}


def check_project(project: Dict[str, Any], workdir: Optional[Path] = None,
                  keep: bool = False) -> List[str]:
    """Prove a generated project works end-to-end: build baseline, pass its
    full fuzz gate, score it. Returns a list of errors (empty == healthy)."""
    spec, files = project["spec"], project["files"]
    ext = LANG_EXT[spec["language"]]
    own = workdir is None
    if own:
        workdir = Path(tempfile.mkdtemp(prefix="kaisen-factory-check-"))
    try:
        for rel, content in files.items():
            p = workdir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

        def step_timeout(script: str) -> float:
            # The self-check IS the trial run: it must respect the project's
            # own step timeouts, not a blanket number.
            steps = spec.get("steps", {})
            if script == "harness/build.py":
                st = steps.get("build") or {}
                return float(st.get("timeout", 120.0))
            if script == "harness/fuzz_verify.py":
                vs = steps.get("verify") or []
                return sum(float(s.get("timeout", 60.0)) for s in vs) or 600.0
            ss = steps.get("score") or []
            return sum(float(s.get("timeout", 300.0)) for s in ss) or 600.0

        def run(script: str, *args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, str(workdir / script), *args],
                capture_output=True, cwd=workdir, timeout=step_timeout(script))

        errors: List[str] = []
        baseline = f"original.{ext}"
        artifact = str(workdir / spec.get("artifact_name", "program"))
        r = run("harness/build.py", baseline, artifact)
        if r.returncode != 0:
            errors.append(f"build failed: {r.stderr.decode(errors='replace')[:300]}")
            return errors
        r = run("harness/fuzz_verify.py", artifact)
        if r.returncode != 0:
            errors.append(
                f"fuzz gate failed on the BASELINE (reference/baseline "
                f"disagreement): {r.stderr.decode(errors='replace')[:300]}")
        r = run("harness/score.py", artifact)
        if r.returncode != 0:
            errors.append(f"score failed: {r.stderr.decode(errors='replace')[:300]}")
        elif "time_ms=" not in r.stdout.decode(errors="replace"):
            errors.append("score produced no time_ms metric")
        return errors
    finally:
        if own and not keep:
            shutil.rmtree(workdir, ignore_errors=True)


NO_TOOLCHAIN = "NO TOOLCHAIN"


def toolchain_available(lang: str) -> bool:
    """True if this machine can build and run `lang` artifacts (compiled:
    a compiler from the registry's candidate list; interpreted: an
    interpreter). Drives create_all's preflight skip."""
    tcs = _LANGS.toolchain_candidates(lang)
    if tcs:
        return any(shutil.which(t) for t in tcs)
    interps = INTERPRETERS.get(lang, ())
    return bool(interps) and any(shutil.which(i) for i in interps)


def create_all(algo_keys: Optional[List[str]] = None,
               langs: Optional[List[str]] = None,
               n_cases: int = 200,
               check: bool = True,
               build_timeout: Optional[float] = None,
               case_timeout: Optional[float] = None) -> List[Dict[str, Any]]:
    """Generate (and self-check) every requested project.

    Returns one report row per project: {"id", "ok", "error", "spec",
    "files"} — spec/files are present only for healthy projects. Languages
    whose toolchain is missing on this machine get a NO_TOOLCHAIN skip row
    (reported, never shipped)."""
    keys = algo_keys or list_algorithms()
    ls = langs or list_languages()
    report: List[Dict[str, Any]] = []
    for key in keys:
        for lang in ls:
            row: Dict[str, Any] = {"id": f"{key}-{lang}", "ok": False,
                                   "error": "", "spec": None, "files": None}
            if not toolchain_available(lang):
                row["error"] = NO_TOOLCHAIN + " on this machine"
                report.append(row)
                continue
            try:
                proj = make_project(key, lang, n_cases=n_cases,
                                    build_timeout=build_timeout,
                                    case_timeout=case_timeout)
            except (KeyError, ValueError) as e:
                row["error"] = str(e)
                report.append(row)
                continue
            if check:
                errs = check_project(proj)
                if errs:
                    row["error"] = "; ".join(errs)
                    report.append(row)
                    continue
            row.update(ok=True, spec=proj["spec"], files=proj["files"])
            report.append(row)
    return report
