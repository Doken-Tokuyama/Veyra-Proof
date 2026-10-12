//! Demo library for the Veyra Proof end-to-end fixture.
//!
//! The CLI in `src/main.rs` is a thin adapter over these functions, so the
//! behavior an end-to-end scenario observes has to come from real logic here.

/// Wrapping sum, kept as the original fixture API.
pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

/// Why a checked operation could not produce a result.
#[derive(Debug, PartialEq, Eq)]
pub enum MathError {
    Overflow,
    DivisionByZero,
}

impl std::fmt::Display for MathError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            MathError::Overflow => write!(f, "arithmetic overflow"),
            MathError::DivisionByZero => write!(f, "division by zero"),
        }
    }
}

/// Sum that reports overflow instead of wrapping in release or panicking in debug.
pub fn checked_add(a: i32, b: i32) -> Result<i32, MathError> {
    a.checked_add(b).ok_or(MathError::Overflow)
}

/// Integer quotient that reports a zero divisor and `i32::MIN / -1` distinctly.
pub fn checked_divide(a: i32, b: i32) -> Result<i32, MathError> {
    if b == 0 {
        return Err(MathError::DivisionByZero);
    }
    a.checked_div(b).ok_or(MathError::Overflow)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adds() {
        assert_eq!(add(1, 2), 3);
    }

    #[test]
    fn checked_add_sums_normal_values() {
        assert_eq!(checked_add(1, 2), Ok(3));
    }

    #[test]
    fn checked_add_reports_overflow_instead_of_wrapping() {
        assert_eq!(checked_add(i32::MAX, 1), Err(MathError::Overflow));
    }

    #[test]
    fn checked_divide_truncates_towards_zero() {
        assert_eq!(checked_divide(7, 2), Ok(3));
    }

    #[test]
    fn checked_divide_rejects_a_zero_divisor() {
        assert_eq!(checked_divide(1, 0), Err(MathError::DivisionByZero));
    }

    #[test]
    fn checked_divide_reports_min_over_neg_one_as_overflow() {
        assert_eq!(checked_divide(i32::MIN, -1), Err(MathError::Overflow));
    }

    #[test]
    fn error_messages_name_the_failure() {
        assert_eq!(MathError::DivisionByZero.to_string(), "division by zero");
        assert_eq!(MathError::Overflow.to_string(), "arithmetic overflow");
    }
}
