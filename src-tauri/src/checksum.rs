// Copyright (C) 2026 OpenMV, LLC.
//
// This software is licensed under terms that can be found in the
// LICENSE file in the root directory of this software component.

// OpenMV CRC16/CRC32

use crc::{Algorithm, Crc, Table};

const OPENMV_CRC16: Algorithm<u16> = Algorithm {
    width: 16,
    poly: 0xF94F,
    init: 0xFFFF,
    refin: false,
    refout: false,
    xorout: 0x0000,
    check: 0x0000,
    residue: 0x0000,
};

const OPENMV_CRC32: Algorithm<u32> = Algorithm {
    width: 32,
    poly: 0xFA567D89,
    init: 0xFFFFFFFF,
    refin: false,
    refout: false,
    xorout: 0x00000000,
    check: 0x00000000,
    residue: 0x00000000,
};

const CRC16: Crc<u16, Table<16>> = Crc::<u16, Table<16>>::new(&OPENMV_CRC16);
const CRC32: Crc<u32, Table<16>> = Crc::<u32, Table<16>>::new(&OPENMV_CRC32);

pub fn calc_crc16(data: &[u8]) -> u16 {
    CRC16.checksum(data)
}

pub fn calc_crc32(data: &[u8]) -> u32 {
    CRC32.checksum(data)
}
