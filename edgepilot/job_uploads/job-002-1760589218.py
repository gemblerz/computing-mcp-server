# fib.py
import time

def fib(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a

start = time.time()
result = fib(100000)
duration = time.time() - start
print(f"Fib done: {result} in {duration:.3f}s")
