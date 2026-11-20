"""40 loops over range() with non-constant arguments (regression: RecursionError in the evaluator)."""

def job_0(xs0, a0, n0, m0, cur):
    for i in range(len(xs0)):
        for j in range(len(xs0), n0):
            cur.execute(f"select * from t0_{i}_{j}")

def job_1(xs1, a1, n1, m1, cur):
    for i in range(n1, m1):
        for j in range(len(xs1), n1):
            cur.execute(f"select * from t1_{i}_{j}")

def job_2(xs2, a2, n2, m2, cur):
    for i in range(len(a2), 0, -1):
        for j in range(len(xs2), n2):
            cur.execute(f"select * from t2_{i}_{j}")

def job_3(xs3, a3, n3, m3, cur):
    for i in range(n3):
        for j in range(len(xs3), n3):
            cur.execute(f"select * from t3_{i}_{j}")

def job_4(xs4, a4, n4, m4, cur):
    for i in range(len(xs4)):
        for j in range(len(xs4), n4):
            cur.execute(f"select * from t4_{i}_{j}")

def job_5(xs5, a5, n5, m5, cur):
    for i in range(n5, m5):
        for j in range(len(xs5), n5):
            cur.execute(f"select * from t5_{i}_{j}")

def job_6(xs6, a6, n6, m6, cur):
    for i in range(len(a6), 0, -1):
        for j in range(len(xs6), n6):
            cur.execute(f"select * from t6_{i}_{j}")

def job_7(xs7, a7, n7, m7, cur):
    for i in range(n7):
        for j in range(len(xs7), n7):
            cur.execute(f"select * from t7_{i}_{j}")

def job_8(xs8, a8, n8, m8, cur):
    for i in range(len(xs8)):
        for j in range(len(xs8), n8):
            cur.execute(f"select * from t8_{i}_{j}")

def job_9(xs9, a9, n9, m9, cur):
    for i in range(n9, m9):
        for j in range(len(xs9), n9):
            cur.execute(f"select * from t9_{i}_{j}")

def job_10(xs10, a10, n10, m10, cur):
    for i in range(len(a10), 0, -1):
        for j in range(len(xs10), n10):
            cur.execute(f"select * from t10_{i}_{j}")

def job_11(xs11, a11, n11, m11, cur):
    for i in range(n11):
        for j in range(len(xs11), n11):
            cur.execute(f"select * from t11_{i}_{j}")

def job_12(xs12, a12, n12, m12, cur):
    for i in range(len(xs12)):
        for j in range(len(xs12), n12):
            cur.execute(f"select * from t12_{i}_{j}")

def job_13(xs13, a13, n13, m13, cur):
    for i in range(n13, m13):
        for j in range(len(xs13), n13):
            cur.execute(f"select * from t13_{i}_{j}")

def job_14(xs14, a14, n14, m14, cur):
    for i in range(len(a14), 0, -1):
        for j in range(len(xs14), n14):
            cur.execute(f"select * from t14_{i}_{j}")

def job_15(xs15, a15, n15, m15, cur):
    for i in range(n15):
        for j in range(len(xs15), n15):
            cur.execute(f"select * from t15_{i}_{j}")

def job_16(xs16, a16, n16, m16, cur):
    for i in range(len(xs16)):
        for j in range(len(xs16), n16):
            cur.execute(f"select * from t16_{i}_{j}")

def job_17(xs17, a17, n17, m17, cur):
    for i in range(n17, m17):
        for j in range(len(xs17), n17):
            cur.execute(f"select * from t17_{i}_{j}")

def job_18(xs18, a18, n18, m18, cur):
    for i in range(len(a18), 0, -1):
        for j in range(len(xs18), n18):
            cur.execute(f"select * from t18_{i}_{j}")

def job_19(xs19, a19, n19, m19, cur):
    for i in range(n19):
        for j in range(len(xs19), n19):
            cur.execute(f"select * from t19_{i}_{j}")

def job_20(xs20, a20, n20, m20, cur):
    for i in range(len(xs20)):
        for j in range(len(xs20), n20):
            cur.execute(f"select * from t20_{i}_{j}")

def job_21(xs21, a21, n21, m21, cur):
    for i in range(n21, m21):
        for j in range(len(xs21), n21):
            cur.execute(f"select * from t21_{i}_{j}")

def job_22(xs22, a22, n22, m22, cur):
    for i in range(len(a22), 0, -1):
        for j in range(len(xs22), n22):
            cur.execute(f"select * from t22_{i}_{j}")

def job_23(xs23, a23, n23, m23, cur):
    for i in range(n23):
        for j in range(len(xs23), n23):
            cur.execute(f"select * from t23_{i}_{j}")

def job_24(xs24, a24, n24, m24, cur):
    for i in range(len(xs24)):
        for j in range(len(xs24), n24):
            cur.execute(f"select * from t24_{i}_{j}")

def job_25(xs25, a25, n25, m25, cur):
    for i in range(n25, m25):
        for j in range(len(xs25), n25):
            cur.execute(f"select * from t25_{i}_{j}")

def job_26(xs26, a26, n26, m26, cur):
    for i in range(len(a26), 0, -1):
        for j in range(len(xs26), n26):
            cur.execute(f"select * from t26_{i}_{j}")

def job_27(xs27, a27, n27, m27, cur):
    for i in range(n27):
        for j in range(len(xs27), n27):
            cur.execute(f"select * from t27_{i}_{j}")

def job_28(xs28, a28, n28, m28, cur):
    for i in range(len(xs28)):
        for j in range(len(xs28), n28):
            cur.execute(f"select * from t28_{i}_{j}")

def job_29(xs29, a29, n29, m29, cur):
    for i in range(n29, m29):
        for j in range(len(xs29), n29):
            cur.execute(f"select * from t29_{i}_{j}")

def job_30(xs30, a30, n30, m30, cur):
    for i in range(len(a30), 0, -1):
        for j in range(len(xs30), n30):
            cur.execute(f"select * from t30_{i}_{j}")

def job_31(xs31, a31, n31, m31, cur):
    for i in range(n31):
        for j in range(len(xs31), n31):
            cur.execute(f"select * from t31_{i}_{j}")

def job_32(xs32, a32, n32, m32, cur):
    for i in range(len(xs32)):
        for j in range(len(xs32), n32):
            cur.execute(f"select * from t32_{i}_{j}")

def job_33(xs33, a33, n33, m33, cur):
    for i in range(n33, m33):
        for j in range(len(xs33), n33):
            cur.execute(f"select * from t33_{i}_{j}")

def job_34(xs34, a34, n34, m34, cur):
    for i in range(len(a34), 0, -1):
        for j in range(len(xs34), n34):
            cur.execute(f"select * from t34_{i}_{j}")

def job_35(xs35, a35, n35, m35, cur):
    for i in range(n35):
        for j in range(len(xs35), n35):
            cur.execute(f"select * from t35_{i}_{j}")

def job_36(xs36, a36, n36, m36, cur):
    for i in range(len(xs36)):
        for j in range(len(xs36), n36):
            cur.execute(f"select * from t36_{i}_{j}")

def job_37(xs37, a37, n37, m37, cur):
    for i in range(n37, m37):
        for j in range(len(xs37), n37):
            cur.execute(f"select * from t37_{i}_{j}")

def job_38(xs38, a38, n38, m38, cur):
    for i in range(len(a38), 0, -1):
        for j in range(len(xs38), n38):
            cur.execute(f"select * from t38_{i}_{j}")

def job_39(xs39, a39, n39, m39, cur):
    for i in range(n39):
        for j in range(len(xs39), n39):
            cur.execute(f"select * from t39_{i}_{j}")

def const_loop(cur):
    for i in range(3):
        cur.execute(f"select * from c_{i}")
