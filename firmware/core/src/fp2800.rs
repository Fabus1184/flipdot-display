//! Column addressing for the FP2800A column driver on the panel.
//!
//! The FP2800A decodes a 5-bit address `A4..A0` into 28 outputs. Within each
//! group of eight addresses (`A2..A0`), code 0 is unused, so the valid
//! addresses are 1–7, 9–15, 17–23 and 25–31.

/// Number of outputs of one FP2800A.
pub const COLUMNS: u8 = 28;

/// The FP2800A address that drives output `column` (0-based).
///
/// # Panics
/// If `column >= 28`.
pub const fn column_address(column: u8) -> u8 {
    assert!(column < COLUMNS);
    let group = column / 7;
    let index = column % 7;
    group * 8 + index + 1
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_datasheet_table() {
        let expected: [u8; 28] = [
            1, 2, 3, 4, 5, 6, 7, //
            9, 10, 11, 12, 13, 14, 15, //
            17, 18, 19, 20, 21, 22, 23, //
            25, 26, 27, 28, 29, 30, 31,
        ];
        for (col, &addr) in expected.iter().enumerate() {
            assert_eq!(column_address(col as u8), addr, "column {col}");
        }
    }

    #[test]
    #[should_panic]
    fn out_of_range_panics() {
        column_address(28);
    }
}
