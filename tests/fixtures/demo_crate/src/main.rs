//! `demo_crate` command line: the shipped entry point that the end-to-end
//! scenarios exercise.
//!
//! Every command routes through the library, so a change in observed behavior
//! must come from real logic rather than from the CLI faking a result.

use demo_crate::{checked_add, checked_divide};
use std::io::Write;
use std::process::ExitCode;

const USAGE: &str = "\
usage: demo_crate <command> [arguments]

commands:
  add <a> <b>      print the checked sum of two integers
  divide <a> <b>   print the integer quotient; errors on zero and on overflow
  concat <w...>    print the words joined by single spaces
  help             print this usage

exit codes:
  0  the command produced its result
  2  the command could not run (bad input, zero divisor, overflow)
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            // Failures are reported, never swallowed: an operator has to be
            // able to read *why* the run did not succeed.
            let stderr = std::io::stderr();
            let mut handle = stderr.lock();
            let _ = writeln!(handle, "error: {message}");
            let _ = writeln!(handle, "{USAGE}");
            ExitCode::from(2)
        }
    }
}

fn run(args: &[String]) -> Result<(), String> {
    let (command, rest) = args.split_first().ok_or("missing command")?;
    match command.as_str() {
        "add" => {
            let (a, b) = two_integers(rest)?;
            let sum = checked_add(a, b).map_err(|err| err.to_string())?;
            println!("{sum}");
            Ok(())
        }
        "divide" => {
            let (a, b) = two_integers(rest)?;
            let quotient = checked_divide(a, b).map_err(|err| err.to_string())?;
            println!("{quotient}");
            Ok(())
        }
        "concat" => {
            if rest.is_empty() {
                return Err("concat requires at least one word".to_string());
            }
            println!("{}", rest.join(" "));
            Ok(())
        }
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown command '{other}'")),
    }
}

fn two_integers(rest: &[String]) -> Result<(i32, i32), String> {
    if rest.len() != 2 {
        return Err(format!(
            "expected exactly two integer arguments, got {}",
            rest.len()
        ));
    }
    Ok((parse_integer(&rest[0])?, parse_integer(&rest[1])?))
}

fn parse_integer(raw: &str) -> Result<i32, String> {
    raw.parse::<i32>()
        .map_err(|_| format!("'{raw}' is not a valid 32-bit integer"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(items: &[&str]) -> Vec<String> {
        items.iter().map(|item| item.to_string()).collect()
    }

    #[test]
    fn no_arguments_is_rejected() {
        assert_eq!(run(&args(&[])), Err("missing command".to_string()));
    }

    #[test]
    fn unknown_command_is_rejected_by_name() {
        let err = run(&args(&["frobnicate"])).unwrap_err();
        assert!(err.contains("unknown command 'frobnicate'"), "{err}");
    }

    #[test]
    fn a_non_numeric_operand_is_rejected() {
        let err = run(&args(&["add", "one", "2"])).unwrap_err();
        assert!(err.contains("not a valid"), "{err}");
    }

    #[test]
    fn a_missing_operand_is_rejected() {
        let err = run(&args(&["add", "1"])).unwrap_err();
        assert!(err.contains("exactly two"), "{err}");
    }

    #[test]
    fn divide_by_zero_is_rejected() {
        let err = run(&args(&["divide", "1", "0"])).unwrap_err();
        assert!(err.contains("division by zero"), "{err}");
    }

    #[test]
    fn concat_rejects_an_empty_word_list() {
        let err = run(&args(&["concat"])).unwrap_err();
        assert!(err.contains("at least one word"), "{err}");
    }
}
