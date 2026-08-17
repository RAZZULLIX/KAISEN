#include <stdio.h>
#include <stdlib.h>

static int is_prime(long long x) {
    if (x < 2) return 0;
    for (long long d = 2; d * d <= x; d++)
        if (x % d == 0) return 0;
    return 1;
}

int main(int argc, char **argv) {
    long long n = argc > 1 ? atoll(argv[1]) : 1000000;
    long long count = 0;
    for (long long i = 2; i < n; i++)
        if (is_prime(i)) count++;
    printf("%lld\n", count);
    return 0;
}
