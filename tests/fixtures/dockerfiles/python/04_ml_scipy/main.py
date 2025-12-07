"""Simple ML computation script."""
import numpy as np
from scipy import linalg

# Simple matrix operations
A = np.array([[1, 2], [3, 4]])
B = np.array([[5, 6], [7, 8]])

print("Matrix multiplication:")
print(np.dot(A, B))

print("\nEigenvalues:")
print(linalg.eigvals(A))

print("Done!")
