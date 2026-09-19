"""Unit tests for the descriptor parser + decision tree."""

import os
import sys
import unittest
from pathlib import Path

# Make the core importable without an installed package.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from itether_core.descriptor import (
    AppleDeviceInfo,
    InterfaceDescriptor,
    TetheringProtocol,
    identify_tethering_function,
    parse_lsusb_dump,
)


SAMPLE_IPHETH = """
Bus 002 Device 005: ID 05ac:12a8 Apple, Inc. iPhone 5/5C/5S/6/SE/7/8/X/XR
Device Descriptor:
  bLength                18
  bDescriptorType         1
  bcdUSB               2.00
  bDeviceClass            0
  bDeviceSubClass         0
  bDeviceProtocol         0
  bMaxPacketSize0        64
  idVendor           0x05ac
  idProduct          0x12a8
  iManufacturer           1 Apple Inc.
  iProduct                2 iPhone
  iSerial                 3 0123456789abcdef0123456789abcdef

Configuration Descriptor:
  bLength                 9
  bDescriptorType         2
  wTotalLength          137
  bNumInterfaces          5
  bConfigurationValue     1
  iConfiguration          0
  bmAttributes         0xc0
  bMaxPower              500mA

  Interface Descriptor:
    bLength                 9
    bDescriptorType         4
    bInterfaceNumber        0
    bAlternateSetting       0
    bNumEndpoints           0
    bInterfaceClass       255 Vendor Specific Class
    bInterfaceSubClass    253
    bInterfaceProtocol      1

  Interface Descriptor:
    bLength                 9
    bDescriptorType         4
    bInterfaceNumber        1
    bAlternateSetting       0
    bNumEndpoints           0
    bInterfaceClass       255 Vendor Specific Class
    bInterfaceSubClass    253
    bInterfaceProtocol      1

  Interface Descriptor:
    bLength                 9
    bDescriptorType         4
    bInterfaceNumber        2
    bAlternateSetting       1
    bNumEndpoints           2
    bInterfaceClass       255 Vendor Specific Class
    bInterfaceSubClass    253
    bInterfaceProtocol      1
    Endpoint Descriptor:
      bLength             7
      bDescriptorType     5
      bEndpointAddress 0x86  EP 6 IN
      bmAttributes            2
        Transfer Type            Bulk
        Synch Type               None
        Usage Type               Data
      wMaxPacketSize     0x0200  1x 512 bytes
    Endpoint Descriptor:
      bLength             7
      bDescriptorType     5
      bEndpointAddress 0x05  EP 5 OUT
      bmAttributes            2
        Transfer Type            Bulk
        Synch Type               None
        Usage Type               Data
      wMaxPacketSize     0x0200  1x 512 bytes

  Interface Descriptor:
    bLength                 9
    bDescriptorType         4
    bInterfaceNumber        3
    bAlternateSetting       0
    bNumEndpoints           0
    bInterfaceClass       255 Vendor Specific Class
    bInterfaceSubClass    253
    bInterfaceProtocol      2

  Interface Descriptor:
    bLength                 9
    bDescriptorType         4
    bInterfaceNumber        4
    bAlternateSetting       0
    bNumEndpoints           0
    bInterfaceClass       255 Vendor Specific Class
    bInterfaceSubClass    253
    bInterfaceProtocol      2
"""


SAMPLE_IOS16_DUAL_NCM = """
Bus 002 Device 005: ID 05ac:12a8 Apple, Inc. iPhone
Device Descriptor:
  bLength                18
  bDescriptorType         1
  bcdUSB               2.10
  bDeviceClass            0
  bDeviceSubClass         0
  bDeviceProtocol         0
  bMaxPacketSize0        64
  idVendor           0x05ac
  idProduct          0x12a8

Configuration Descriptor:
  bNumInterfaces          6

  Interface Descriptor:
    bLength                 9
    bDescriptorType         4
    bInterfaceNumber        2
    bAlternateSetting       0
    bNumEndpoints           1
    bInterfaceClass         2 Communications
    bInterfaceSubClass     13
    bInterfaceProtocol      0
    iInterface             16 NCM Control
    CDC Union:
      bMasterInterface        2
      bSlaveInterface         3
    CDC Header:
      bcdCDC               1.10
    CDC Ethernet:
    CDC NCM:
      bcdNcmVersion        1.00
      bmNetworkCapabilities 0x3b
    Endpoint Descriptor:
      bLength             7
      bDescriptorType     5
      bEndpointAddress 0x86  EP 6 IN
      bmAttributes            3
        Transfer Type            Interrupt
        Synch Type               None
        Usage Type               Data
      wMaxPacketSize     0x0010  1x 16 bytes
      bInterval              11

  Interface Descriptor:
    bLength                 9
    bDescriptorType         4
    bInterfaceNumber        3
    bAlternateSetting       1
    bNumEndpoints           2
    bInterfaceClass        10 CDC Data
    bInterfaceSubClass      0
    bInterfaceProtocol      1
    Endpoint Descriptor:
      bEndpointAddress 0x87  EP 7 IN
      bmAttributes            2
        Transfer Type            Bulk
      wMaxPacketSize     0x0200  1x 512 bytes
    Endpoint Descriptor:
      bEndpointAddress 0x05  EP 5 OUT
      bmAttributes            2
        Transfer Type            Bulk
      wMaxPacketSize     0x0200  1x 512 bytes

  Interface Descriptor:
    bLength                 9
    bDescriptorType         4
    bInterfaceNumber        4
    bAlternateSetting       0
    bNumEndpoints           1
    bInterfaceClass         2 Communications
    bInterfaceSubClass     13
    bInterfaceProtocol      0
    iInterface             19 NCM Control
    CDC Union:
      bMasterInterface        4
      bSlaveInterface         5
    Endpoint Descriptor:
      bEndpointAddress 0x88  EP 8 IN
      bmAttributes            3
        Transfer Type            Interrupt
      wMaxPacketSize     0x0010  1x 16 bytes
      bInterval              11

  Interface Descriptor:
    bLength                 9
    bDescriptorType         4
    bInterfaceNumber        5
    bAlternateSetting       1
    bNumEndpoints           2
    bInterfaceClass        10 CDC Data
    bInterfaceSubClass      0
    bInterfaceProtocol      1
    Endpoint Descriptor:
      bEndpointAddress 0x89  EP 9 IN
      bmAttributes            2
        Transfer Type            Bulk
      wMaxPacketSize     0x0200  1x 512 bytes
    Endpoint Descriptor:
      bEndpointAddress 0x06  EP 6 OUT
      bmAttributes            2
        Transfer Type            Bulk
      wMaxPacketSize     0x0200  1x 512 bytes
"""


SAMPLE_IOS17_PRIVATE_NCM = """
Bus 002 Device 005: ID 05ac:12a8 Apple, Inc. iPhone
Configuration Descriptor:
  bNumInterfaces          6

  Interface Descriptor:
    bLength                 9
    bDescriptorType         4
    bInterfaceNumber        2
    bAlternateSetting       0
    bNumEndpoints           1
    bInterfaceClass       255 Vendor Specific Class
    bInterfaceSubClass    253
    bInterfaceProtocol      1
    Endpoint Descriptor:
      bEndpointAddress 0x86  EP 6 IN
      bmAttributes            3
        Transfer Type            Interrupt
      wMaxPacketSize     0x0010  1x 16 bytes

  Interface Descriptor:
    bLength                 9
    bDescriptorType         4
    bInterfaceNumber        3
    bAlternateSetting       1
    bNumEndpoints           2
    bInterfaceClass        10 CDC Data
    bInterfaceSubClass      0
    bInterfaceProtocol      1
    Endpoint Descriptor:
      bEndpointAddress 0x87  EP 7 IN
      bmAttributes            2
        Transfer Type            Bulk
      wMaxPacketSize     0x0200  1x 512 bytes
    Endpoint Descriptor:
      bEndpointAddress 0x05  EP 5 OUT
      bmAttributes            2
        Transfer Type            Bulk
      wMaxPacketSize     0x0200  1x 512 bytes
"""


class DescriptorParserTests(unittest.TestCase):
    def test_parse_ipheth(self) -> None:
        devs = parse_lsusb_dump(SAMPLE_IPHETH)
        self.assertEqual(len(devs), 1)
        d = devs[0]
        self.assertEqual(d.vendor_id, 0x05AC)
        self.assertEqual(d.product_id, 0x12A8)
        self.assertTrue(d.is_known_tethering_device)
        cand = identify_tethering_function(d)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.protocol, TetheringProtocol.IPHETH)
        self.assertEqual(cand.control_interface, 2)

    def test_parse_ios16_dual_ncm_picks_interrupt_one(self) -> None:
        devs = parse_lsusb_dump(SAMPLE_IOS16_DUAL_NCM)
        d = devs[0]
        cand = identify_tethering_function(d)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.protocol, TetheringProtocol.CDC_NCM)
        self.assertEqual(cand.control_interface, 2)
        self.assertEqual(cand.data_interface, 3)
        self.assertTrue(cand.rejected_remotexpc)
        self.assertTrue(cand.has_interrupt_endpoint)

    def test_parse_ios17_private_ncm(self) -> None:
        # iOS 17 exposes the NCM control function with vendor-specific class
        # 0xFF subclass 0xFD but still an interrupt-IN endpoint. Our parser
        # treats it as a standard NCM function (interrupt endpoint present).
        devs = parse_lsusb_dump(SAMPLE_IOS17_PRIVATE_NCM)
        d = devs[0]
        cand = identify_tethering_function(d)
        self.assertIsNotNone(cand)
        # We pick the one with the interrupt-IN endpoint; both protocol
        # classifications are accepted as long as the bind succeeds.
        self.assertIn(
            cand.protocol,
            (TetheringProtocol.CDC_NCM, TetheringProtocol.CDC_NCM_PRIVATE),
        )


class NonAppleTests(unittest.TestCase):
    def test_non_apple_device_rejected(self) -> None:
        d = AppleDeviceInfo(vendor_id=0x1234, product_id=0x5678)
        self.assertFalse(d.is_apple)
        self.assertIsNone(identify_tethering_function(d))

    def test_known_pid_but_no_descriptors_yet(self) -> None:
        d = AppleDeviceInfo(vendor_id=0x05AC, product_id=0x12A8, interfaces=())
        self.assertTrue(d.is_known_tethering_device)
        # No descriptors -> cannot pick a tethering function yet.
        self.assertIsNone(identify_tethering_function(d))

    def test_mac_pid_recognised(self) -> None:
        d = AppleDeviceInfo(vendor_id=0x05AC, product_id=0x1905)
        self.assertTrue(d.is_known_tethering_device)


class ResolutionTests(unittest.TestCase):
    """Make sure we never pick the RemoteXPC function."""

    def _build_macos_layout(self, remote_intr=True) -> AppleDeviceInfo:
        """macOS-style config 4 layout (iOS 16.4+, iPad Air 2 tested)."""
        iface_a = InterfaceDescriptor(
            number=2,
            class_code=0x02,
            subclass=0x0D,
            protocol=0x00,
            has_interrupt_in=remote_intr,
            interrupt_in_ep=0x86,
            cdc_union_slave=3,
            is_ncm_function=True,
        )
        iface_b = InterfaceDescriptor(
            number=3,
            class_code=0x0A,
            subclass=0x00,
            protocol=0x01,
            bulk_in_ep=0x87,
            bulk_out_ep=0x05,
        )
        iface_c = InterfaceDescriptor(
            number=4,
            class_code=0x02,
            subclass=0x0D,
            protocol=0x00,
            has_interrupt_in=True,
            interrupt_in_ep=0x88,
            cdc_union_slave=5,
            is_ncm_function=True,
        )
        iface_d = InterfaceDescriptor(
            number=5,
            class_code=0x0A,
            subclass=0x00,
            protocol=0x01,
            bulk_in_ep=0x89,
            bulk_out_ep=0x06,
        )
        return AppleDeviceInfo(
            vendor_id=0x05AC,
            product_id=0x12A8,
            interfaces=(iface_a, iface_b, iface_c, iface_d),
        )

    def test_interrupt_bearing_function_wins(self) -> None:
        d = self._build_macos_layout(remote_intr=True)
        cand = identify_tethering_function(d)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.control_interface, 2)
        self.assertEqual(cand.data_interface, 3)
        self.assertTrue(cand.rejected_remotexpc)

    def test_lower_numbered_function_wins_when_equally_qualified(self) -> None:
        # Both interfaces have an interrupt-IN endpoint. We must pick the
        # lower-numbered one (Apple's convention).
        d = self._build_macos_layout(remote_intr=True)
        cand = identify_tethering_function(d)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.control_interface, 2)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
