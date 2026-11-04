//! A deliberately unfinished crate.
//!
//! It exists so the live audit can prove it rejects a production `todo!()`
//! through the real toolchain, instead of only through a mocked runner.

/// Returns the answer, once somebody implements it.
pub fn answer() -> u32 {
    todo!("the feature is not implemented")
}

/// Adds two integers.
pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn add_works() {
        assert_eq!(add(2, 2), 4);
    }

    #[test]
    fn answer_is_implemented() {
        // Calls the production placeholder on purpose: this test must fail,
        // and cargo's exit code is part of what the audit has to report.
        assert_eq!(answer(), 42);
    }

    #[test]
    fn add_has_no_observable_assertion() {
        let _ = add(1, 1);
    }
}
