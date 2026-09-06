#!/usr/bin/env python3
"""Verify factory baselines: build+run each (algo, lang) source against the
trusted Python reference on a few inputs.  Usage:
    python3 tools/verify_factory_baselines.py <extra_module.py> [--all]
A baseline source is correct only if it COMPILES with the project's build
script AND prints the reference output for every sampled input.
"""
import importlib.util, os, subprocess, sys, tempfile, pathlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kaisen import factory as F
from kaisen.languages import ext_from_lang, toolchain_candidates

# ---- trusted reference (python) ----
def _intlist(s):
    return [int(x) for x in s.strip().split()] if s.strip() else []

def ref(algo, args):
    a = [list(x) for x in args]
    if algo == "prime-count": n=int(a[0][0]); return str(_prime_count(n))
    if algo == "popcount": x=int(a[0][0]); return str(bin(x).count("1") if x>0 else 0)
    if algo == "gcd": x=int(a[0][0]); y=int(a[1][0]); return str(_gcd(x,y))
    if algo == "fib-mod": n=int(a[0][0]); return str(_fib(n))
    if algo == "num-divisors": n=int(a[0][0]); return str(_ndiv(n))
    if algo == "collatz-steps": n=int(a[0][0]); return str(_collatz(n))
    if algo == "sum-range": n=int(a[0][0]); return str(sum(range(1,n+1))%1000000007 if n>=1 else 0)
    if algo == "reverse-str": return a[0][0][::-1]
    if algo == "is-palindrome": s=a[0][0]; return "1" if s==s[::-1] else "0"
    if algo == "rle":
        s=a[0][0]; out=[]; i=0
        while i<len(s):
            j=i
            while j<len(s) and s[j]==s[i]: j+=1
            out.append(s[i]+str(j-i)); i=j
        return "".join(out)
    if algo == "is-prime": n=int(a[0][0]); return "1" if _isprime(n) else "0"
    if algo == "int-sqrt": n=int(a[0][0]); return str(_isqrt(n))
    if algo == "digital-root":
        n=int(a[0][0])
        while n>=10: n=sum(int(d) for d in str(n))
        return str(n)
    if algo == "trailing-zeros": n=int(a[0][0]); return str(_tz(n))
    if algo == "omega": n=int(a[0][0]); return str(_omega(n))
    if algo == "nth-prime": k=int(a[0][0]); return str(_nthprime(k))
    if algo == "levenshtein": return str(_lev(a[0][0], a[1][0]))
    if algo == "lcs-length": return str(_lcs(a[0][0], a[1][0]))
    if algo == "lpal-len": return str(_lpal(a[0][0]))
    if algo == "kmp-prefix": return " ".join(str(x) for x in _kmp(a[0][0]))
    if algo == "caesar-shift": return _caesar(a[0][0])
    if algo == "happy-steps": return str(_happy(int(a[0][0])))
    if algo == "modexp": return str(_modexp(int(a[0][0]), int(a[1][0]), int(a[2][0])))
    if algo == "max-subarray":
        xs=_intlist(a[0][0]); best=0
        for i in range(len(xs)):
            s=0
            for j in range(i,len(xs)): s+=xs[j]; best=max(best,s)
        return str(best) if xs else "0"
    if algo == "count-inversions":
        xs=_intlist(a[0][0]); c=0
        for i in range(len(xs)):
            for j in range(i+1,len(xs)):
                if xs[i]>xs[j]: c+=1
        return str(c)
    raise KeyError(algo)

def _prime_count(n):
    if n<2: return 0
    s=[True]*n; s[0]=s[1]=False
    for i in range(2,int(n**0.5)+1):
        if s[i]:
            for j in range(i*i,n,i): s[j]=False
    return sum(s)
def _gcd(x,y):
    while y: x,y=y,x%y
    return x
def _fib(n):
    a,b=0,1
    for _ in range(n): a,b=b,(a+b)%1000000007
    return a
def _ndiv(n):
    c=0; d=1
    while d*d<=n: c+= 1 if d*d==n else 2 if n%d==0 else 0; d+=1
    return c
def _collatz(n):
    s=0
    while n>1:
        n=n//2 if n%2==0 else 3*n+1; s+=1
    return s
def _isprime(n):
    if n<2: return False
    d=2
    while d*d<=n:
        if n%d==0: return False; d+=1
    return True
def _isqrt(n):
    lo,hi=0,n+1
    while hi-lo>1:
        mid=(lo+hi)//2
        if mid*mid<=n: lo=mid
        else: hi=mid
    return lo
def _tz(n):
    c=0
    while n>0 and n%2==0: c+=1; n//=2
    return c
def _omega(n):
    c=0; d=2
    while d*d<=n:
        while n%d==0: c+=1; n//=d
        d+=1
    if n>1: c+=1
    return c
def _nthprime(k):
    c=0; x=1
    while c<k:
        x+=1
        if _isprime(x): c+=1
    return x
def _lev(a,b):
    prev=list(range(len(b)+1))
    for i in range(1,len(a)+1):
        cur=[i]+[0]*len(b)
        for j in range(1,len(b)+1):
            sub=prev[j-1]+(0 if a[i-1]==b[j-1] else 1)
            cur[j]=min(prev[j]+1, cur[j-1]+1, sub)
        prev=cur
    return prev[-1]
def _lcs(a,b):
    prev=[0]*(len(b)+1)
    for i in range(1,len(a)+1):
        cur=[0]*(len(b)+1)
        for j in range(1,len(b)+1):
            cur[j]=prev[j-1]+1 if a[i-1]==b[j-1] else max(prev[j],cur[j-1])
        prev=cur
    return prev[-1]
def _lpal(s):
    n=len(s); best=0
    def ispal(i,j):
        while i<j:
            if s[i]!=s[j]: return False
            i+=1; j-=1
        return True
    for i in range(n):
        for j in range(i,n):
            if j-i+1>best and ispal(i,j): best=j-i+1
    return best
def _kmp(s):
    pi=[0]*len(s)
    for i in range(1,len(s)):
        j=pi[i-1]
        while j>0 and s[i]!=s[j]: j=pi[j-1]
        if s[i]==s[j]: j+=1
        pi[i]=j
    return pi
def _caesar(s):
    out=[]
    for ch in s:
        c=ord(ch)
        if 97<=c<=122: out.append(chr(97+(c-97+3)%26))
        elif 65<=c<=90: out.append(chr(65+(c-65+3)%26))
        else: out.append(ch)
    return "".join(out)
def _happy(n):
    seen=set(); steps=0
    while n!=1:
        if n in seen: break
        seen.add(n); n=sum(int(d)**2 for d in str(n)); steps+=1
    return steps
def _modexp(a,b,m):
    if m==1: return 0
    r=1%m; a%=m
    while b:
        if b&1: r=r*a%m
        a=a*a%m; b>>=1
    return r

# ---- sample inputs per algorithm (from the factory workload families) ----
SAMPLES = {
 "prime-count": [["10"],["100"],["1000"]], "popcount": [["7"],["255"],["1024"]],
 "gcd": [["12","8"],["17","5"],["48","36"]], "fib-mod": [["0"],["10"],["50"]],
 "num-divisors": [["1"],["12"],["100"]], "collatz-steps": [["1"],["27"],["50"]],
 "sum-range": [["0"],["10"],["100"]], "reverse-str": [["abc"],["hello"],["racecar"]],
 "is-palindrome": [["racecar"],["abc"],["abba"]], "rle": [["aabbb"],["abc"],["aa"]],
 "is-prime": [["2"],["97"],["100"]], "int-sqrt": [["0"],["17"],["100"]],
 "digital-root": [["0"],["12345"],["987654321"]], "trailing-zeros": [["0"],["8"],["12"]],
 "omega": [["1"],["12"],["30"]], "nth-prime": [["1"],["10"],["100"]],
 "levenshtein": [["kitten","sitting"],["abc","acb"],["","abc"]],
 "lcs-length": [["abcde","ace"],["abc","abc"],["ab","ba"]],
 "lpal-len": [["babad"],["cbbd"],["a"]], "kmp-prefix": [["aabaab"],["ababca"],["a"]],
 "caesar-shift": [["abc"],["xyz"],["Hello, World!"]],
 "happy-steps": [["1"],["19"],["4"]],
 "modexp": [["2","10","1000"],["3","7","4"],["123456789","0","97"]],
 "max-subarray": [["-2 1 -3 4 -1 2 1 -5 4"],["1 2 3"],["-1 -2 -3"]],
 "count-inversions": [["3 1 2"],["1 2 3"],["3 2 1"]],
}

def main():
    modpath = sys.argv[1]
    spec = importlib.util.spec_from_file_location("extra", modpath)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    extra = getattr(m, "EXTRA_BASELINES", {})
    fails = 0; total = 0
    for algo, langs in extra.items():
        for lang, src in langs.items():
            total += 1
            if not src.strip(): continue
            ok = check_one(algo, lang, src)
            if not ok: fails += 1; print(f"FAIL {algo}/{lang}")
    print(f"\n=== {total-fails}/{total} PASS ({fails} fail) ===")
    sys.exit(0 if fails==0 else 1)

def check_one(algo, lang, src):
    ext = ext_from_lang(lang)
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td); orig = td / f"original{ext}"; orig.write_text(src)
        bpath = td / "build.py"; bpath.write_text(F._BUILD_SCRIPTS[lang])
        art = str(td / "program")
        r = subprocess.run([sys.executable, str(bpath), str(orig), art],
                           capture_output=True, timeout=180)
        if r.returncode != 0:
            print(f"  BUILD FAIL {algo}/{lang}: {r.stderr.decode()[:180]}")
            return False
        for case in SAMPLES.get(algo, []):
            r2 = subprocess.run([art]+case, capture_output=True, timeout=60,
                                env={**os.environ, "PATH": "/usr/local/bin:/opt/swift/usr/bin:"+os.environ.get("PATH","")})
            got = r2.stdout.decode().strip()
            # ref(algo, args) expects args = [[v1], [v2], ...] (one list per argv)
            exp = ref(algo, [[v] for v in case])
            if got != exp:
                print(f"  MISMATCH {algo}/{lang} in={case} got={got!r} exp={exp!r} rc={r2.returncode}")
                return False
    return True

if __name__ == "__main__":
    main()
