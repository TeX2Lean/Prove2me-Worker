import Formal.SmokeTest

namespace Formal

-- Deliberately false synthetic statement; the worker must reject this proof.
theorem expected_failure (n : Nat) : n + 1 = n := by
  rfl

end Formal
