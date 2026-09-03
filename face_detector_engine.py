# -*- coding: utf-8 -*-
"""
SmartVision Robust Biometric Face Detection & Recognition Engine
-----------------------------------------------------------------
Features:
1. Low-light & dark contrast auto-enhancement using LAB-space CLAHE
   and adaptive gamma luminance compensation.
2. Multi-tiered cascade detection (Standard HOG -> Enhanced HOG ->
   Upsampled HOG -> CNN MMOD detector).
3. ResNet-34 128-dimensional deep metric embeddings extraction.
4. Strictly calibrated Euclidean distance matching (tau = 0.58-0.60)
   ensuring registered identities (e.g. Shivam Vishwakarma) are accurately
   recognized while non-registered individuals are strictly designated as 'Unknown Face'.
"""

import os
import cv2
import numpy as np

try:
    import face_recognition
except ImportError:
    face_recognition = None


def enhance_image_for_detection(img_np):
    """
    Enhances contrast and brightness for robust face detection in low light,
    shadows, or uneven illumination.
    
    Args:
        img_np (np.ndarray): RGB uint8 image array.
        
    Returns:
        tuple: (enhanced_rgb: np.ndarray, is_low_light: bool, mean_brightness: float)
    """
    if img_np is None or not isinstance(img_np, np.ndarray) or img_np.size == 0:
        return img_np, False, 128.0

    try:
        # Convert RGB to LAB color space for luminance-specific processing
        lab = cv2.cvtColor(img_np, cv2.COLOR_RGB2LAB)
        l, a, b = cv2.split(lab)
        mean_l = float(np.mean(l))
        is_low_light = mean_l < 115.0

        if is_low_light:
            # Adaptive gamma correction to brighten shadows without blowing out highlights
            gamma = 0.50 if mean_l < 50.0 else (0.62 if mean_l < 85.0 else 0.75)
            inv_gamma = 1.0 / gamma
            table = np.array([((i / 255.0) ** inv_gamma) * 255 for i in np.arange(0, 256)]).astype('uint8')
            l = cv2.LUT(l, table)

            # High-contrast CLAHE for dark/shadowed environments
            clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        else:
            # Mild CLAHE for normal lighting to balance shadows
            clahe = cv2.createCLAHE(clipLimit=1.6, tileGridSize=(8, 8))

        l_clahe = clahe.apply(l)
        enhanced_lab = cv2.merge((l_clahe, a, b))
        enhanced_rgb = cv2.cvtColor(enhanced_lab, cv2.COLOR_LAB2RGB)
        return enhanced_rgb, is_low_light, mean_l

    except Exception as e:
        print(f"[FaceEngine Warning] Image enhancement error: {e}")
        return img_np, False, 128.0


def detect_face_locations_robust(img_np, enable_cnn=False, fast_live_mode=False):
    """
    Multi-stage face detection pipeline optimized for low-latency real-time inference:
      Stage 1: Fast HOG on raw RGB (takes ~15-25ms; succeeds on standard lighting)
      Stage 2: HOG on CLAHE/Gamma enhanced RGB (recovers faces in dim/dark rooms)
      Stage 3: HOG with 2x upsampling on enhanced RGB (recovers small/distant faces) - skipped in fast_live_mode
      Stage 4: Distance Scale-Up pass (1.4x scaling for small/far faces) - skipped in fast_live_mode
      Stage 5: Dlib CNN MMOD detector (only when explicitly enabled)
    
    Returns:
        tuple: (face_locations: list of (top, right, bottom, left), enhanced_rgb: np.ndarray)
    """
    if face_recognition is None or img_np is None or img_np.size == 0:
        return [], img_np

    # Stage 1: Standard HOG on original RGB (fastest for standard lighting, ~15-25ms)
    try:
        locs = face_recognition.face_locations(img_np, number_of_times_to_upsample=1, model="hog")
        if locs:
            return locs, img_np
    except Exception as e:
        print(f"[FaceEngine Stage 1 HOG]: {e}")

    # Stage 2: Low-light / dark / contrast enhancement (only if Stage 1 found 0 faces)
    enhanced_rgb, is_low_light, mean_l = enhance_image_for_detection(img_np)

    try:
        locs = face_recognition.face_locations(enhanced_rgb, number_of_times_to_upsample=1, model="hog")
        if locs:
            return locs, enhanced_rgb
    except Exception as e:
        print(f"[FaceEngine Stage 2 Enhanced HOG]: {e}")

    # In fast live video stream mode, return immediately to maintain ultra-fast FPS and instant face disappear feedback
    if fast_live_mode:
        return [], enhanced_rgb

    # Stage 3: HOG with 2x upsampling on enhanced image (recovers small/distant faces)
    try:
        locs = face_recognition.face_locations(enhanced_rgb, number_of_times_to_upsample=2, model="hog")
        if locs:
            return locs, enhanced_rgb
    except Exception as e:
        print(f"[FaceEngine Stage 3 Upsampled HOG]: {e}")

    # Stage 4: Distance Scale-Up pass (upscales distant/small faces 1.4x for high distance recall)
    try:
        h, w = enhanced_rgb.shape[:2]
        if max(h, w) <= 960:
            scale_factor = 1.4
            scaled_img = cv2.resize(enhanced_rgb, (0, 0), fx=scale_factor, fy=scale_factor, interpolation=cv2.INTER_CUBIC)
            locs_scaled = face_recognition.face_locations(scaled_img, number_of_times_to_upsample=1, model="hog")
            if locs_scaled:
                locs = [
                    (
                        int(t / scale_factor),
                        int(r / scale_factor),
                        int(b / scale_factor),
                        int(l / scale_factor)
                    )
                    for (t, r, b, l) in locs_scaled
                ]
                return locs, enhanced_rgb
    except Exception as e:
        print(f"[FaceEngine Stage 4 Distance Upscale]: {e}")

    # Stage 5: Dlib CNN MMOD Detector (maximum accuracy under challenging conditions)
    if enable_cnn:
        try:
            target_img = enhanced_rgb if is_low_light else img_np
            # Downsample slightly if image is very large for CNN speed
            h, w = target_img.shape[:2]
            if max(h, w) > 960:
                scale = 960.0 / float(max(h, w))
                target_cnn = cv2.resize(target_img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
                cnn_locs = face_recognition.face_locations(target_cnn, number_of_times_to_upsample=0, model="cnn")
                # Scale boxes back
                locs = [(int(t / scale), int(r / scale), int(b / scale), int(l / scale)) for (t, r, b, l) in cnn_locs]
            else:
                locs = face_recognition.face_locations(target_img, number_of_times_to_upsample=0, model="cnn")
            if locs:
                return locs, enhanced_rgb
        except Exception as e:
            print(f"[FaceEngine Stage 5 CNN]: {e}")

    return [], enhanced_rgb


def get_face_biometrics_robust(img_np, known_face_locations=None, enable_cnn=False, fast_live_mode=False):
    """
    Detects faces and generates 128-d ResNet-34 deep feature embeddings.
    If face encoding extraction fails on raw image (due to darkness or low contrast),
    it automatically falls back to the contrast-enhanced image.
    
    Returns:
        tuple: (face_locations: list, face_encodings: list)
    """
    if face_recognition is None or img_np is None or img_np.size == 0:
        return [], []

    enhanced_rgb = None
    if known_face_locations is None or len(known_face_locations) == 0:
        face_locations, enhanced_rgb = detect_face_locations_robust(img_np, enable_cnn=enable_cnn, fast_live_mode=fast_live_mode)
    else:
        face_locations = known_face_locations
        enhanced_rgb, _, _ = enhance_image_for_detection(img_np)

    if not face_locations:
        return [], []

    # Extract 128-d ResNet-34 embeddings for all located faces
    face_encodings = []
    for loc in face_locations:
        enc = None
        # Try raw image first
        try:
            encs = face_recognition.face_encodings(img_np, [loc], num_jitters=1)
            if encs and len(encs) > 0:
                enc = encs[0]
        except Exception:
            enc = None

        # Fallback to enhanced image if raw extraction failed (darkness/contrast issue)
        if enc is None and enhanced_rgb is not None:
            try:
                encs = face_recognition.face_encodings(enhanced_rgb, [loc], num_jitters=1)
                if encs and len(encs) > 0:
                    enc = encs[0]
            except Exception:
                enc = None

        face_encodings.append(enc)

    return face_locations, face_encodings


def match_face_encoding(candidate_encoding, known_encodings_list, tolerance=0.58):
    """
    Matches candidate 128-d embedding against known encodings using Euclidean distance.
    Returns:
        tuple: (best_index: int or None, min_distance: float, is_match: bool, confidence_str: str)
    """
    if candidate_encoding is None or not known_encodings_list or face_recognition is None:
        return None, 1.0, False, "0%"

    try:
        distances = face_recognition.face_distance(known_encodings_list, candidate_encoding)
        if len(distances) == 0:
            return None, 1.0, False, "0%"

        best_idx = int(np.argmin(distances))
        min_dist = float(distances[best_idx])

        if min_dist < tolerance:
            confidence = max(0, min(100, int((1.0 - min_dist) * 100)))
            return best_idx, min_dist, True, f"{confidence}%"
        else:
            return None, min_dist, False, "0%"

    except Exception as e:
        print(f"[FaceEngine Matching Error]: {e}")
        return None, 1.0, False, "0%"
