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

LANG_EXT = {"c": "c", "python": "py", "rust": "rs", "go": "go"}
LANG_LABEL = {"c": "C", "python": "Python", "rust": "Rust", "go": "Go"}


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
pi = []
for i in range(len(s)):
    j = pi[-1] if pi else 0
    while j > 0 and s[i] != s[j]:
        j = pi[j - 1]
    if s[i] == s[j]:
        j += 1
    pi.append(j)
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
    for (size_t i = 0; i < n; i++) {
        int j = i ? pi[i - 1] : 0;
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
pi = []
for i in range(len(s)):
    j = pi[-1] if pi else 0
    while j > 0 and s[i] != s[j]:
        j = pi[j - 1]
    if s[i] == s[j]:
        j += 1
    pi.append(j)
print(" ".join(map(str, pi)))
''',
        "rust": '''\
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let s: String = args.get(1).cloned().unwrap_or_default();
    let v: Vec<char> = s.chars().collect();
    let mut pi: Vec<i32> = Vec::new();
    for i in 0..v.len() {
        let mut j = if i > 0 { pi[i - 1] as usize } else { 0 };
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


# --------------------------------------------------------------------------- #
# harness templates
# --------------------------------------------------------------------------- #

_BUILD_SCRIPTS: Dict[str, str] = {
    "c": '''\
#!/usr/bin/env python3
import subprocess, sys
cand, art = sys.argv[1], sys.argv[2]
r = subprocess.run(["gcc", "-O2", "-Werror=implicit-function-declaration", "-o", art, cand], capture_output=True)
if r.returncode != 0:
    sys.stderr.write(r.stderr.decode(errors="replace")[:4000])
    sys.exit(1)
print("OK")
''',
    "python": '''\
#!/usr/bin/env python3
import shutil, stat, sys
cand, art = sys.argv[1], sys.argv[2]
shutil.copyfile(cand, art)
# Shebang insurance: an LLM candidate that drops the shebang would exec as
# "Exec format error"; the build step owns executability, the model owns logic.
with open(art, "rb") as f:
    first = f.readline(64)
if not first.startswith(b"#!"):
    data = open(cand, "rb").read()
    with open(art, "wb") as f:
        f.write(b"#!/usr/bin/env python3\\n")
        f.write(data)
st = __import__("os").stat(art)
__import__("os").chmod(art, st.st_mode | stat.S_IEXEC)
print("OK")
''',
    "rust": '''\
#!/usr/bin/env python3
import subprocess, sys
cand, art = sys.argv[1], sys.argv[2]
r = subprocess.run(["rustc", "-O", "-o", art, cand], capture_output=True)
if r.returncode != 0:
    sys.stderr.write(r.stderr.decode(errors="replace")[:4000])
    sys.exit(1)
print("OK")
''',
    "go": '''\
#!/usr/bin/env python3
import subprocess, sys
cand, art = sys.argv[1], sys.argv[2]
r = subprocess.run(["go", "build", "-o", art, cand], capture_output=True)
if r.returncode != 0:
    sys.stderr.write(r.stderr.decode(errors="replace")[:4000])
    sys.exit(1)
print("OK")
''',
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

def make_project(algo_key: str, lang: str, n_cases: int = 200,
                 seed: Optional[int] = None) -> Dict[str, Any]:
    """Generate spec + files for one (algorithm, language) project.

    Returns {"id", "spec", "files"} — or raises ValueError/KeyError with a
    precise message. Reference outputs are computed from the trusted Python
    reference; the baseline is NOT trusted until check_project proves it."""
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
            "compare": algo["compare"], "case_timeout": 10.0,
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
                      "args": ["{candidate}", "{artifact}"], "timeout": 180},
            "verify": [{"program": "harness/fuzz_verify.py",
                        "args": ["{artifact}"], "timeout": 600}],
            "score": [{"program": "harness/score.py",
                       "args": ["{artifact}"], "timeout": 900,
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

        def run(script: str, *args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, str(workdir / script), *args],
                capture_output=True, cwd=workdir, timeout=1200)

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


def create_all(algo_keys: Optional[List[str]] = None,
               langs: Optional[List[str]] = None,
               n_cases: int = 200,
               check: bool = True) -> List[Dict[str, Any]]:
    """Generate (and self-check) every requested project.

    Returns one report row per project: {"id", "ok", "error", "spec",
    "files"} — spec/files are present only for healthy projects."""
    keys = algo_keys or list_algorithms()
    ls = langs or list_languages()
    report: List[Dict[str, Any]] = []
    for key in keys:
        for lang in ls:
            row: Dict[str, Any] = {"id": f"{key}-{lang}", "ok": False,
                                   "error": "", "spec": None, "files": None}
            try:
                proj = make_project(key, lang, n_cases=n_cases)
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
