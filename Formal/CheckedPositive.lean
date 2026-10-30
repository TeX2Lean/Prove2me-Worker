import Formal.SmokeTest

namespace Formal

-- Synthetic regression fixture: depends on the existing public smoke theorem.
theorem checked_positive (n : Nat) : n + 0 = n := smoke n

end Formal
